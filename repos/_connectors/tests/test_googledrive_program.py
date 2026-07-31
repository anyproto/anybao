"""programs/googleDrive@v1 through the REAL guest kernel (connectorenv
wires anybao's tests/kernelenv.py at this repo's programs/). Fixtures
are inline dicts modeled on Meet REST v2 / Drive v3 replies. Pinned
here: the envelope shapes ({ok, conferences|transcripts|files, ...} —
records pass through with Google's own camelCase names, C1; derived
keys docId/exportUri/transcriptName/fileId), the shared managed
credential on every request, entry paging + "Speaker: text" joining,
the WORKSPACE-ONLY 403 mapping, the googleAuth not-connected mapping,
the 429 backoff/give-up message, and the plain-text export body."""

import json

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}


def ok(body):
    return {"status": 200, "headers": {}, "body": json.dumps(body), "url": ""}


CONFERENCE = {"name": "conferenceRecords/abc123",
              "startTime": "2026-07-30T10:00:00Z",
              "endTime": "2026-07-30T10:45:00Z",
              "space": "spaces/xyz",
              "expireTime": "2026-08-29T10:45:00Z"}

TRANSCRIPT = {"name": "conferenceRecords/abc123/transcripts/t1",
              "state": "ENDED",
              "startTime": "2026-07-30T10:00:05Z",
              "endTime": "2026-07-30T10:44:50Z",
              "docsDestination": {
                  "document": "doc123",
                  "exportUri": "https://docs.google.com/document/d/doc123"}}


class FakeGoogle:
    """Routes http.get on a url substring — first match wins; asserts
    the shared managed googleAuth credential on every request. An
    Exception value is raised host-side → guest EffectError (kernelenv
    encodes it as "<type name>: <message>")."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.sleeps = []

    def __call__(self, name, payload):
        if name == "sleep":
            self.sleeps.append(payload["seconds"])
            return {}
        assert name == "http.get", f"unexpected effect {name}"
        assert payload["credential"] == CRED
        self.requests.append(payload)
        for pattern, resp in self.routes:
            if pattern in payload["url"]:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for url: {payload['url']}")


def load(routes):
    fake = FakeGoogle(routes)
    gd = connector_kernel(effect=fake).use("googleDrive@v1")
    return gd, fake


# ---- Meet primary path ------------------------------------------------------

def test_list_conferences_shape():
    gd, fake = load([("conferenceRecords?", ok(
        {"conferenceRecords": [CONFERENCE], "nextPageToken": "tok1"}))])
    out = gd.list_conferences()
    # conferences is the derived top-level key; records pass through verbatim
    assert out == {"ok": True, "conferences": [CONFERENCE],
                   "nextPageToken": "tok1"}
    url = fake.requests[0]["url"]
    assert url.startswith("https://meet.googleapis.com/v2/conferenceRecords")
    assert "pageSize=25" in url  # default page size


def test_get_transcript_text_pages_and_joins():
    page1 = ok({"transcriptEntries": [
        {"participant": {"signedinUser": {"displayName": "Alice"}},
         "text": "hello"}],
        "nextPageToken": "tok2"})
    page2 = ok({"transcriptEntries": [
        {"participant": {"anonymousUser": {"displayName": "Guest"}},
         "content": "hi there"}]})
    gd, fake = load([("pageToken=tok2", page2), ("/entries", page1)])
    out = gd.get_transcript_text("conferenceRecords/abc/transcripts/t1")
    assert out == {"ok": True,
                   "transcriptName": "conferenceRecords/abc/transcripts/t1",
                   "text": "Alice: hello\nGuest: hi there",
                   "lines": ["Alice: hello", "Guest: hi there"],
                   "entryCount": 2, "truncated": False}
    assert len(fake.requests) == 2
    assert "conferenceRecords/abc/transcripts/t1/entries" in fake.requests[0]["url"]


def test_recent_meeting_transcripts_derived_keys():
    gd, _ = load([
        ("/transcripts?", ok({"transcripts": [TRANSCRIPT]})),
        ("conferenceRecords?", ok({"conferenceRecords": [CONFERENCE]})),
    ])
    out = gd.recent_meeting_transcripts()
    assert out == {"ok": True, "nextPageToken": None, "meetings": [{
        "conferenceRecord": "conferenceRecords/abc123",
        "startTime": "2026-07-30T10:00:00Z",
        "endTime": "2026-07-30T10:45:00Z",
        "space": "spaces/xyz",
        "transcripts": [{
            "name": "conferenceRecords/abc123/transcripts/t1",
            "state": "ENDED",
            "startTime": "2026-07-30T10:00:05Z",
            "endTime": "2026-07-30T10:44:50Z",
            # docId/exportUri derive from docsDestination.document/.exportUri
            "docId": "doc123",
            "exportUri": "https://docs.google.com/document/d/doc123"}],
        "transcriptsError": None}]}


# ---- error mapping ----------------------------------------------------------

def test_403_maps_to_workspace_only_help():
    body = {"error": {"message": "The caller does not have permission",
                      "status": "PERMISSION_DENIED"}}
    gd, _ = load([("conferenceRecords", {"status": 403, "headers": {},
                                         "body": json.dumps(body), "url": ""})])
    out = gd.list_conferences()
    assert out["ok"] is False and out["status"] == 403
    assert "WORKSPACE-ONLY" in out["error"]
    assert "organizer" in out["error"]
    assert "drive.readonly" in out["error"]
    assert "The caller does not have permission" in out["error"]


def test_not_connected_maps_to_connect_help():
    not_connected = type("not_connected", (Exception,), {})
    gd, _ = load([("conferenceRecords", not_connected(
        "google is not connected — run the provider's connect first"))])
    out = gd.list_conferences()
    assert out["ok"] is False
    assert "connect()" in out["error"]


def test_429_backoff_and_rate_message():
    gd, fake = load([("conferenceRecords",
                      {"status": 429, "headers": {}, "body": "", "url": ""})])
    out = gd.list_conferences()
    assert out["ok"] is False
    assert "60 requests/min" in out["error"]
    assert len(fake.requests) == 4 and len(fake.sleeps) == 3


# ---- Drive fallback ---------------------------------------------------------

def test_find_transcript_docs_default_query():
    files = [{"id": "doc123", "name": "Standup - Transcript",
              "createdTime": "2026-05-02T09:00:00Z",
              "webViewLink": "https://docs.google.com/document/d/doc123/view"}]
    gd, fake = load([("drive/v3/files", ok(
        {"files": files, "nextPageToken": None}))])
    out = gd.find_transcript_docs(folder_id="fold1")
    assert out == {"ok": True, "files": files, "nextPageToken": None}
    url = fake.requests[0]["url"]
    assert url.startswith("https://www.googleapis.com/drive/v3/files?")
    assert "Transcript" in url  # default name-contains clause
    assert "fold1" in url       # folder scoping
    assert "createdTime" in url  # trim fields + orderBy


def test_export_doc_text_plain_body():
    gd, fake = load([("/export", {"status": 200, "headers": {},
                                  "body": "Alice: hello\nBob: bye", "url": ""})])
    out = gd.export_doc_text("doc123")
    # the export body is plain text, not JSON — passed through verbatim
    assert out == {"ok": True, "fileId": "doc123",
                   "text": "Alice: hello\nBob: bye"}
    url = fake.requests[0]["url"]
    assert "files/doc123/export" in url
    assert "mimeType=text%2Fplain" in url


# ---- input validation -------------------------------------------------------

def test_input_validation_no_request():
    gd, fake = load([])
    out = gd.list_transcripts("")
    assert out["ok"] is False and "conference_record_name" in out["error"]
    out = gd.get_transcript_text(None)
    assert out["ok"] is False and "transcript_name" in out["error"]
    out = gd.export_doc_text("")
    assert out["ok"] is False and "file_id" in out["error"]
    assert fake.requests == []
