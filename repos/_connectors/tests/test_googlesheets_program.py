"""programs/googleSheets@v1 through the REAL guest kernel
(connectorenv wires anybao's tests/kernelenv.py at this repo's
programs/). Fixtures are inline dicts modeled on Sheets REST v4 /
Drive v3 replies. Pinned here: Drive-discovery files passing through
verbatim (id/name/modifiedTime, C1), get_spreadsheet's documented
derived keys (flattened title, gridProperties → rows/cols), the
{range, values} / valueRanges envelopes, managed-ref credential
plumbing on every request, the typed not-connected EffectError
mapping to googleAuth._friendly's connect() guidance, and the
25-range batch cap rejecting before anything crosses the boundary."""

import json

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}

# The host's typed failure contract (ADR-011): EffectError arrives as
# "type: message" — kernelenv builds it from the raised exception's
# CLASS NAME + str, so an exception class literally named
# not_connected yields the prefix the connector matches on.
NotConnected = type("not_connected", (Exception,), {})


def ok(payload, status=200, headers=None):
    return {"status": status, "headers": headers or {},
            "body": json.dumps(payload), "url": ""}


class FakeGoogle:
    """Serves http.get + sleep from ordered (url-substring → response)
    routes — first match wins, so specific routes go first. Asserts
    the managed credential ref on every http request; an Exception
    value is raised host-side → guest EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.slept = []

    def __call__(self, name, payload):
        if name == "sleep":
            self.slept.append(payload["seconds"])
            return {"slept": payload["seconds"]}
        assert name == "http.get", f"unexpected effect {name}"
        # tokens never cross the boundary — only the managed ref does
        assert payload["credential"] == CRED
        self.requests.append(payload)
        url = payload["url"]
        for pattern, resp in self.routes:
            if pattern in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {url}")


def load(routes):
    fake = FakeGoogle(routes)
    gs = connector_kernel(effect=fake).use("googleSheets@v1")
    return gs, fake


# ---- discovery (Drive) + credential plumbing --------------------------------

def test_list_spreadsheets_files_verbatim_and_credential():
    gs, fake = load([("drive/v3/files", ok({
        "files": [
            {"id": "1BxSheetA", "name": "Budget 2026",
             "modifiedTime": "2026-07-29T10:12:00.000Z"},
            {"id": "1BxSheetB", "name": "OKR tracker",
             "modifiedTime": "2026-07-20T08:00:00.000Z"}],
        "nextPageToken": "drive-page-2"}))])
    out = gs.list_spreadsheets()
    # Drive file stubs pass through verbatim — id/name/modifiedTime (C1)
    assert out == {"ok": True, "files": [
        {"id": "1BxSheetA", "name": "Budget 2026",
         "modifiedTime": "2026-07-29T10:12:00.000Z"},
        {"id": "1BxSheetB", "name": "OKR tracker",
         "modifiedTime": "2026-07-20T08:00:00.000Z"}],
        "nextPageToken": "drive-page-2"}
    req = fake.requests[0]
    assert req["credential"] == CRED
    url = req["url"]
    assert url.startswith("https://www.googleapis.com/drive/v3/files?")
    assert "pageSize=100" in url  # default clamp
    assert "orderBy=modifiedTime%20desc" in url
    assert _unquoted(url).count("mimeType='application/vnd.google-apps.spreadsheet'") == 1

    gs.list_spreadsheets(query="Q3'26")
    # name fragment ANDed in, single quotes escaped per Drive grammar
    assert "name contains 'Q3\\'26'" in _unquoted(fake.requests[1]["url"])


def _unquoted(url):
    out = url
    for code, ch in (("%20", " "), ("%27", "'"), ("%3D", "="), ("%5C", "\\"),
                     ("%2F", "/"), ("%3A", ":")):
        out = out.replace(code, ch)
    return out


def test_not_connected_surfaces_friendly_connect_message():
    gs, _ = load([("drive/v3/files", NotConnected(
        "google is not connected — run the provider's connect first"))])
    out = gs.list_spreadsheets()
    assert out["ok"] is False
    assert out["error"].startswith("not_connected: google is not connected")
    assert "connect()" in out["error"]


# ---- spreadsheet metadata ---------------------------------------------------

def test_get_spreadsheet_derives_rows_cols_from_grid_properties():
    gs, fake = load([("spreadsheets/1BxSheetA?", ok({
        "spreadsheetId": "1BxSheetA",
        "properties": {"title": "Budget 2026"},
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Sheet1",
                            "gridProperties": {"rowCount": 1000,
                                               "columnCount": 26}}},
            {"properties": {"sheetId": 419, "title": "Pivot",
                            "gridProperties": {"rowCount": 80,
                                               "columnCount": 12}}}]}))])
    out = gs.get_spreadsheet("1BxSheetA")
    # rows/cols are the documented derived flattening of gridProperties
    assert out == {"ok": True, "spreadsheetId": "1BxSheetA",
                   "title": "Budget 2026",
                   "sheets": [{"title": "Sheet1", "sheetId": 0,
                               "rows": 1000, "cols": 26},
                              {"title": "Pivot", "sheetId": 419,
                               "rows": 80, "cols": 12}]}
    assert "fields=" in fake.requests[0]["url"]  # metadata-only mask


def test_get_spreadsheet_requires_id():
    gs, fake = load([])
    out = gs.get_spreadsheet("")
    assert out["ok"] is False and "spreadsheet_id is required" in out["error"]
    assert fake.requests == []  # nothing crossed the boundary


# ---- values -----------------------------------------------------------------

def test_get_values_envelope_and_render_options():
    body = {"range": "Sheet1!A1:B3", "majorDimension": "ROWS",
            "values": [["Name", "Score"], ["ada", "10"], ["bob", "7"]]}
    gs, fake = load([("/values/Sheet1", ok(body))])
    out = gs.get_values("1BxSheetA", "Sheet1!A1:B3")
    # range/values verbatim; majorDimension trimmed away
    assert out == {"ok": True, "range": "Sheet1!A1:B3",
                   "values": [["Name", "Score"], ["ada", "10"], ["bob", "7"]]}
    assert "valueRenderOption=FORMATTED_VALUE" in fake.requests[0]["url"]

    gs.get_values("1BxSheetA", "Sheet1!A1:B3", unformatted=True)
    assert "valueRenderOption=UNFORMATTED_VALUE" in fake.requests[1]["url"]


def test_get_values_requires_range():
    gs, fake = load([])
    out = gs.get_values("1BxSheetA", "")
    assert out["ok"] is False and "range is required" in out["error"]
    assert "A1 notation" in out["error"]
    assert fake.requests == []


def test_batch_get_values_trims_value_ranges():
    gs, fake = load([("/values:batchGet", ok({
        "spreadsheetId": "1BxSheetA",
        "valueRanges": [
            {"range": "Sheet1!A1:A2", "majorDimension": "ROWS",
             "values": [["Name"], ["ada"]]},
            {"range": "Pivot!B1:B1", "majorDimension": "ROWS"}]}))])
    out = gs.batch_get_values("1BxSheetA", ["Sheet1!A1:A2", "Pivot!B1:B1"])
    assert out == {"ok": True, "valueRanges": [
        {"range": "Sheet1!A1:A2", "values": [["Name"], ["ada"]]},
        {"range": "Pivot!B1:B1", "values": []}]}
    url = fake.requests[0]["url"]
    # list params repeat the key — ?ranges=a&ranges=b style
    assert "ranges=Sheet1%21A1%3AA2" in url and "ranges=Pivot%21B1%3AB1" in url


def test_batch_get_values_caps_ranges_at_25():
    gs, fake = load([])
    out = gs.batch_get_values("1BxSheetA", [f"Sheet1!A{i}" for i in range(26)])
    assert out["ok"] is False
    assert "too many ranges (26)" in out["error"] and "cap is 25" in out["error"]
    assert fake.requests == []  # rejected before the boundary


# ---- records view -----------------------------------------------------------

def test_read_sheet_as_objects_pads_ragged_rows():
    body = {"range": "Sheet1!A1:C3",
            "values": [["Name", "", "Score"], ["ada", "x", "10"], ["bob"]]}
    gs, _ = load([("/values/Sheet1", ok(body))])
    out = gs.read_sheet_as_objects("1BxSheetA", "Sheet1")
    # empty header cell → positional colN key; short rows pad with ""
    assert out == {"ok": True, "range": "Sheet1!A1:C3",
                   "headers": ["Name", "col2", "Score"],
                   "records": [{"Name": "ada", "col2": "x", "Score": "10"},
                               {"Name": "bob", "col2": "", "Score": ""}]}


def test_read_sheet_as_objects_header_row_offset():
    body = {"range": "Sheet1!A1:B3",
            "values": [["ignore", "me"], ["Name", "Score"], ["ada", "10"]]}
    gs, _ = load([("/values/Sheet1", ok(body))])
    out = gs.read_sheet_as_objects("1BxSheetA", "Sheet1", header_row=2)
    assert out["headers"] == ["Name", "Score"]
    assert out["records"] == [{"Name": "ada", "Score": "10"}]
