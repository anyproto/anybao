"""Google Meet transcripts (primary) + Drive Doc-export fallback, read-only.

Meet REST v2 lists conference records and pages transcript entries into
"Speaker: text" lines. WORKSPACE-ONLY: needs a paid Workspace plan,
admin-enabled transcription, and the caller must have been the meeting
ORGANIZER — personal Gmail authorizes but returns nothing; Meet artifacts
are deleted ~30 days after the meeting. Older meetings: the transcript
Google Doc persists in Drive — find_transcript_docs/export_doc_text cover
them but need the RESTRICTED drive.readonly scope (NOT in the default
union): re-consent via googleAuth.connect(scopes=[...]).
Methods return {ok, ...} or {ok: False, error}."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Auth rides googleAuth@v1 (one consent covers the Google family):
# tokens never enter guest code — every request names the managed
# `credential: {ref: "connector.oauth.google", ...}` and the broker
# refreshes/injects host-side (anybao ADR-011 §6). Trim, don't rename
# (C1): kept fields carry Meet/Drive's own names (name, state,
# startTime, endTime, space, docsDestination, id, createdTime,
# webViewLink). Derived keys — `conferences` (Meet's top-level
# conferenceRecords[]), `docId`/`exportUri` (flattened
# docsDestination.document/.exportUri), `transcriptName`/`text`/
# `lines`/`entryCount`/`truncated`, `fileId` — are named in each
# method's docstring. Budgets: Meet allows ~60 req/min per project;
# Drive export caps at 10 MB per Doc.

_auth = use("googleAuth@v1")  # noqa: F821 - guest global
_CRED = _auth._CRED

_MEET_BASE = "https://meet.googleapis.com/v2"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100        # Meet conferenceRecords/transcripts ceiling
_ENTRIES_PAGE_SIZE = 100    # Meet entries: default 10, max 100
_MAX_ENTRIES = 2000         # hard cap on entries assembled per transcript
_MAX_ENTRY_PAGES = 50       # page-loop safety valve
_DRIVE_PAGE_SIZE = 100      # Drive files.list default (max 1000)
_DRIVE_MAX_PAGE_SIZE = 1000
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_AUTH_PREFIXES = ("not_connected", "oauth_reconsent_required",
                  "not_configured", "consent_timeout")


def _clamp(n, default, cap):
    if not isinstance(n, (int, float)) or n <= 0:
        return default
    return min(int(n), cap)


def _quote(s):
    """Percent-encode for a URL query value (no urllib in the kernel)."""
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(c if c in safe else "".join(f"%{b:02X}" for b in c.encode()) for c in str(s))


def _qs(params):
    """?k=v&k=v query string; list values repeat the key;
    None/"" skipped. The broker's `params` kwarg can't repeat keys,
    hence guest-side assembly."""
    parts = []
    for k, v in params.items():
        if v is None or v == "" or v == []:
            continue
        for item in v if isinstance(v, list) else [v]:
            if item is None or item == "":
                continue
            parts.append(f"{_quote(k)}={_quote(item)}")
    return "?" + "&".join(parts) if parts else ""


def _api_msg(resp):
    try:
        return (resp.json().get("error") or {}).get("message") or f"HTTP {resp.status}"
    except (ValueError, AttributeError):
        return f"HTTP {resp.status}"


def _get(url, raw=False):
    """One authed GET with bounded 429 backoff → {ok, body} |
    {ok: False, error, status?}. raw=True returns resp.text verbatim
    (Drive export bodies are plain text, not JSON)."""
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
                        "error": "Google rejected the token (HTTP 401) — run "
                                 "googleAuth.connect() in a user-facing turn to reauthorize."}
            if resp.status == 403:
                return {"ok": False, "status": 403,
                        "error": f"Google denied access (HTTP 403): {_api_msg(resp)}. "
                                 "The Meet API is WORKSPACE-ONLY — it needs a paid Google "
                                 "Workspace plan with admin-enabled transcription, and you must "
                                 "have been the meeting organizer; it returns nothing on a "
                                 "personal Gmail account. The Drive fallback additionally needs "
                                 "the RESTRICTED drive.readonly scope (not in the default "
                                 "union) — re-consent via googleAuth.connect(scopes=[..., "
                                 '"https://www.googleapis.com/auth/drive.readonly"]).'}
            if resp.status == 429:
                return {"ok": False, "status": 429,
                        "error": f"Google rate limit (HTTP 429): {_api_msg(resp)}. The Meet "
                                 "API allows ~60 requests/min per project — gave up after "
                                 f"{_MAX_RETRIES} retries."}
            return {"ok": False, "status": resp.status,
                    "error": f"Google error: {_api_msg(resp)}"}
        if raw:
            return {"ok": True, "body": resp.text}
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "error": "Google returned a non-JSON body"}
    return {"ok": False, "error": "unreachable"}  # loop always returns or retries


def _entry_speaker(e):
    """Best-effort display name for a TranscriptEntry — Meet keys the
    participant under signedinUser / anonymousUser.displayName /
    phoneUser.displayName (or a bare resource-name string); field
    drift across docs is handled by probing the common shapes."""
    if not isinstance(e, dict):
        return "Unknown"
    if isinstance(e.get("speaker"), str) and e["speaker"]:
        return e["speaker"]
    p = e.get("participant")
    if isinstance(p, dict):
        signed = p.get("signedinUser") or {}
        if signed.get("displayName"):
            return signed["displayName"]
        if signed.get("user"):
            return signed["user"]
        for k in ("anonymousUser", "phoneUser"):
            u = p.get(k) or {}
            if u.get("displayName"):
                return u["displayName"]
    if isinstance(p, str) and p:
        return p
    return "Unknown"


def _entry_text(e):
    """The spoken text — Meet docs show both `text` and `content`."""
    if not isinstance(e, dict):
        return ""
    if isinstance(e.get("text"), str):
        return e["text"]
    if isinstance(e.get("content"), str):
        return e["content"]
    return ""


# ---- Meet REST v2 (PRIMARY) -------------------------------------------------

@span("googleDrive.list_conferences", kind="getter")  # noqa: F821 - guest global
def list_conferences(page_size=None, page_token=None, filter=None):
    """Recent Meet conference records → {ok, conferences, nextPageToken}.

    conferences is the derived top-level key for Meet's
    conferenceRecords[]; each record passes through verbatim (name,
    startTime, endTime, space). filter is the Meet list filter (e.g.
    by space.name or start time — see the Meet API docs). page_size
    defaults 25, caps at 100. WORKSPACE-ONLY: empty on personal Gmail;
    records are deleted ~30 days after the meeting."""
    r = _get(_MEET_BASE + "/conferenceRecords" + _qs({
        "pageSize": _clamp(page_size, _DEFAULT_PAGE_SIZE, _MAX_PAGE_SIZE),
        "pageToken": page_token, "filter": filter}))
    if not r["ok"]:
        return r
    body = r["body"] or {}
    return {"ok": True, "conferences": body.get("conferenceRecords") or [],
            "nextPageToken": body.get("nextPageToken")}


@span("googleDrive.list_transcripts", kind="getter")  # noqa: F821 - guest global
def list_transcripts(conference_record_name):
    """Transcripts of one conference record → {ok, transcripts, nextPageToken}.

    Accepts the full "conferenceRecords/{id}" resource name or a bare
    id. Each transcript passes through verbatim: name, state,
    startTime, endTime, docsDestination {document, exportUri} — the
    underlying Google Doc id + browser link."""
    if not conference_record_name or not isinstance(conference_record_name, str):
        return {"ok": False,
                "error": 'conference_record_name is required (e.g. "conferenceRecords/abc123")'}
    rec = (conference_record_name if conference_record_name.startswith("conferenceRecords/")
           else "conferenceRecords/" + conference_record_name)
    r = _get(_MEET_BASE + "/" + rec + "/transcripts" + _qs({"pageSize": _MAX_PAGE_SIZE}))
    if not r["ok"]:
        return r
    body = r["body"] or {}
    return {"ok": True, "transcripts": body.get("transcripts") or [],
            "nextPageToken": body.get("nextPageToken")}


@span("googleDrive.get_transcript_text", kind="getter")  # noqa: F821 - guest global
def get_transcript_text(transcript_name):
    """Whole transcript paged into "Speaker: text" lines → {ok, text, ...}.

    transcript_name is the full resource name
    "conferenceRecords/{r}/transcripts/{t}" (from list_transcripts).
    Full shape: {ok, transcriptName, text, lines, entryCount,
    truncated} — all derived keys (raw Meet transcriptEntries never
    return). Follows nextPageToken at ≤100 entries/page; caps at 2000
    entries / 50 pages — truncated=True means a cap hit, not an error.
    Speaker names are best-effort (Meet's entry fields drift)."""
    if not transcript_name or not isinstance(transcript_name, str):
        return {"ok": False,
                "error": 'transcript_name is required '
                         '(e.g. "conferenceRecords/abc/transcripts/xyz")'}
    lines = []
    entry_count = 0
    truncated = False
    page_token = None
    pages = 0
    while True:
        r = _get(_MEET_BASE + "/" + transcript_name + "/entries"
                 + _qs({"pageSize": _ENTRIES_PAGE_SIZE, "pageToken": page_token}))
        if not r["ok"]:
            return r
        body = r["body"] or {}
        for e in body.get("transcriptEntries") or body.get("entries") or []:
            if entry_count >= _MAX_ENTRIES:
                truncated = True
                break
            lines.append(f"{_entry_speaker(e)}: {_entry_text(e)}")
            entry_count += 1
        pages += 1
        page_token = body.get("nextPageToken")
        if truncated or not page_token or pages >= _MAX_ENTRY_PAGES:
            if page_token and not truncated:
                truncated = True  # hit the page-loop cap
            break
    return {"ok": True, "transcriptName": transcript_name, "text": "\n".join(lines),
            "lines": lines, "entryCount": entry_count, "truncated": truncated}


@span("googleDrive.recent_meeting_transcripts", kind="getter")  # noqa: F821 - guest global
def recent_meeting_transcripts(page_size=None, page_token=None, filter=None):
    """Recent conferences + transcript summaries → {ok, meetings, nextPageToken}.

    meetings: [{conferenceRecord, startTime, endTime, space,
    transcripts: [{name, state, startTime, endTime, docId, exportUri}],
    transcriptsError}] — conferenceRecord derives from the record's
    name; docId/exportUri derive from Meet's docsDestination.document/
    .exportUri. Does NOT fetch entry text (one request per conference
    already) — pick a transcript and call get_transcript_text."""
    listed = list_conferences(page_size=page_size, page_token=page_token, filter=filter)
    if not listed["ok"]:
        return listed
    meetings = []
    for c in listed["conferences"]:
        c = c or {}
        tr = list_transcripts(c.get("name"))
        transcripts = []
        if tr["ok"]:
            for t in tr["transcripts"]:
                t = t or {}
                dd = t.get("docsDestination") or {}
                transcripts.append({"name": t.get("name"), "state": t.get("state"),
                                    "startTime": t.get("startTime"),
                                    "endTime": t.get("endTime"),
                                    "docId": dd.get("document"),
                                    "exportUri": dd.get("exportUri")})
        meetings.append({"conferenceRecord": c.get("name"), "startTime": c.get("startTime"),
                         "endTime": c.get("endTime"), "space": c.get("space"),
                         "transcripts": transcripts,
                         "transcriptsError": None if tr["ok"] else tr["error"]})
    return {"ok": True, "meetings": meetings, "nextPageToken": listed["nextPageToken"]}


# ---- Drive v3 export (FALLBACK — needs restricted drive.readonly) -----------

@span("googleDrive.find_transcript_docs", kind="getter")  # noqa: F821 - guest global
def find_transcript_docs(q=None, folder_id=None, name_contains=None,
                         page_size=None, page_token=None):
    """Find Meet-transcript Google Docs in Drive → {ok, files, nextPageToken}.

    FALLBACK for meetings older than the Meet API's ~30-day retention —
    the transcript Doc persists in Drive indefinitely. Default query
    matches Docs whose name contains "Transcript"; narrow with
    name_contains / folder_id (the "Meet Recordings" folder), or pass a
    raw Drive q. files keep Drive's names: id, name, createdTime,
    webViewLink. Needs the RESTRICTED drive.readonly scope (NOT in the
    default union) — re-consent via googleAuth.connect(scopes=[...,
    "https://www.googleapis.com/auth/drive.readonly"])."""
    if not q:
        clauses = ["mimeType='application/vnd.google-apps.document'"]
        contains = str(name_contains or "Transcript").replace("'", "\\'")
        clauses.append(f"name contains '{contains}'")
        if folder_id:
            clauses.append(f"'{folder_id}' in parents")
        q = " and ".join(clauses)
    r = _get(_DRIVE_BASE + "/files" + _qs({
        "q": q, "pageSize": _clamp(page_size, _DRIVE_PAGE_SIZE, _DRIVE_MAX_PAGE_SIZE),
        "pageToken": page_token,
        "fields": "nextPageToken,files(id,name,createdTime,webViewLink)",
        "orderBy": "createdTime desc"}))
    if not r["ok"]:
        return r
    body = r["body"] or {}
    return {"ok": True, "files": body.get("files") or [],
            "nextPageToken": body.get("nextPageToken")}


@span("googleDrive.export_doc_text", kind="getter")  # noqa: F821 - guest global
def export_doc_text(file_id):
    """Export a Google Doc as plain text → {ok, fileId, text} (derived keys).

    Drive v3 files/{id}/export?mimeType=text/plain — the response body
    IS the text (not JSON). Works for transcript Docs of any age and
    generic Docs. Drive's export cap is 10 MB — larger Docs fail with a
    Google error, surfaced as {ok: False}. Needs the RESTRICTED
    drive.readonly scope — re-consent via googleAuth.connect(scopes=[...,
    "https://www.googleapis.com/auth/drive.readonly"])."""
    if not file_id or not isinstance(file_id, str):
        return {"ok": False, "error": "file_id is required"}
    r = _get(_DRIVE_BASE + "/files/" + _quote(file_id) + "/export"
             + _qs({"mimeType": "text/plain"}), raw=True)
    if not r["ok"]:
        return r
    return {"ok": True, "fileId": file_id, "text": r["body"] or ""}


def main(args):
    return list_conferences()
