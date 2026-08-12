"""programs/gmailSync@v1 through the REAL guest kernel (ADR-012).

Pinned: the five clean_html passes on synthetic mail (fixtures with
real bodies are personal and stay out of git), the chunked full-sync
slice (idempotency skip, checkpoint-after-commit, profile historyId
captured BEFORE listing), the cooperative fuel governor, coalesced
incremental apply (adds/labels/deletes), the 404-cursor fallback, and
the duplicate-sync_state dance (freshest wins, stale deleted)."""

import base64
import json
import re

from connectorenv import connector_kernel

# --- fakes -------------------------------------------------------------------

class FakeAny:
    """The agent:any@v1 surface gmailSync touches, dict-backed."""

    def __init__(self, emails=None, states=None):
        self.emails = dict(emails or {})       # gmail_id -> object row
        self.states = list(states or [])       # sync_state object rows
        self.types_created = []
        self.deleted = []
        self.markdowns = {}
        self.updates = []
        self._n = 0

    # -- catalog
    def list_types(self, space):
        return [{"id": "T_EMAIL", "xKey": "email"},
                {"id": "T_STATE", "xKey": "sync_state"}]

    def create_type(self, space, body):
        self.types_created.append(body["xKey"])
        return {"typeId": "T_" + body["xKey"], "xKey": body["xKey"],
                "created": False, "addedProps": {}}

    # -- objects
    def query_objects(self, space, filter=None, limit=None, **kw):
        f = filter or {}
        if f.get("any.types") == "sync_state":
            return list(self.states)
        if f.get("any.types") == "email":
            return list(self.emails.values())
        gid = f.get("email.gmail_id")
        if isinstance(gid, dict):
            wanted = set(gid.get("$in") or [])
            return [r for g, r in self.emails.items() if g in wanted]
        if gid is not None:
            return [self.emails[gid]] if gid in self.emails else []
        return []

    def create_object(self, space, body):
        self._n += 1
        oid = f"obj{self._n}"
        props = (body.get("initialProperties") or {})
        if "email" in props:
            self.emails[props["email"]["gmail_id"]] = {
                "id": oid, "email": dict(props["email"])}
        if "sync_state" in props:
            self.states.append({"id": oid,
                                "sync_state": dict(props["sync_state"]),
                                "modifiedAt": 100})
        return {"objectId": oid}

    def update_object(self, space, oid, body):
        self.updates.append((oid, body))
        for row in self.states:
            if row["id"] == oid and "sync_state" in body:
                row["sync_state"].update(body["sync_state"])
        return {"objectId": oid}

    def delete_object(self, space, oid):
        self.deleted.append(oid)
        self.emails = {g: r for g, r in self.emails.items() if r["id"] != oid}
        self.states = [r for r in self.states if r["id"] != oid]
        return None

    def put_markdown(self, space, oid, content):
        self.markdowns[oid] = content
        return {}

    # convenience for asserts
    def state(self):
        return self.states[0]["sync_state"] if self.states else None


def b64url(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def raw_msg(mid, subject="s", html="<p>hello</p>", labels=("INBOX",)):
    return {"id": mid, "threadId": "t" + mid, "labelIds": list(labels),
            "internalDate": "1786000000000", "snippet": "snip " + mid,
            "payload": {"mimeType": "multipart/alternative", "headers": [
                {"name": "From", "value": f"{mid}@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Tue, 12 Aug 2026 10:00:00 +0200"},
            ], "parts": [{"mimeType": "text/html",
                          "body": {"data": b64url(html)}}]}}


def batch_response(raws, requested_body):
    ids = re.findall(r"messages/(\w+)\?format=", requested_body)
    parts = []
    for mid in ids:
        if mid in raws:
            parts.append("--BB\r\nContent-Type: application/http\r\n\r\n"
                         "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                         + json.dumps(raws[mid]) + "\r\n")
    return {"status": 200, "url": "",
            "headers": {"content-type": "multipart/mixed; boundary=BB"},
            "body": "".join(parts) + "--BB--\r\n"}


def gmail_fx(raws, pages=None, history=None, history_status=200, fuel=None):
    """http/fuel/sleep effects for one scenario. pages: list of
    {"messages":[ids], "nextPageToken"?}; history: reply dict."""
    calls = []

    def fx(name, payload):
        calls.append((name, payload))
        if name == "fuel.state":
            return {"remaining": (fuel.pop(0) if fuel else 50_000_000_000),
                    "budget": 50_000_000_000}
        if name == "sleep":
            return {}
        url = payload.get("url", "")
        if name == "http.get":
            if "/profile" in url:
                return {"status": 200, "url": "", "headers": {},
                        "body": json.dumps({"historyId": "H100"})}
            if "/history" in url:
                if history_status != 200:
                    return {"status": history_status, "url": "", "headers": {},
                            "body": json.dumps({"error": {"message": "expired"}})}
                return {"status": 200, "url": "", "headers": {},
                        "body": json.dumps(history)}
            if "/messages" in url:
                page = pages.pop(0)
                return {"status": 200, "url": "", "headers": {},
                        "body": json.dumps({
                            "messages": [{"id": i, "threadId": "t" + i}
                                         for i in page["messages"]],
                            "nextPageToken": page.get("nextPageToken"),
                            "resultSizeEstimate": 201})}
        if name == "http.post" and "batch/gmail" in url:
            return batch_response(raws, payload.get("body") or "")
        raise AssertionError(f"unhandled {name} {url}")

    fx.calls = calls
    return fx


def load(fx, fake):
    app = connector_kernel(effect=fx, any_client=fake)
    return app.use("gmailSync@v1")


# --- clean_html --------------------------------------------------------------

_TRACKER_LINK = ('<a href="https://click.example-tracker.com/r?u='
                 'https%3A%2F%2Fshop.example.com%2Forder%2F9%3Futm_source%3Dmail"'
                 ">your order</a>")
MAIL_HTML = """
<html><head><style>.x{}</style></head><body>
<div style="display:none;max-height:0">preheader teaser junk</div>
<table><tr><td><h1>Invoice ready</h1></td><td>Total: <b>€42</b></td></tr></table>
<img src="pixel.gif" width="1" height="1">
""" + _TRACKER_LINK + """
<a href="https://real.example.com/page?utm_source=nl&x=1">docs</a>
<div class="gmail_quote">On Tue, someone wrote: old quoted reply</div>
<p>Thanks!</p>
<p>--</p>
<p>Jane Doe, CEO</p>
</body></html>
"""


def test_clean_html_five_passes():
    fake = FakeAny()
    mod = load(gmail_fx({}), fake)
    out = mod.clean_html(MAIL_HTML)
    md, sig = out["markdown"], out["signature"]
    assert "preheader" not in md            # hidden block dropped
    assert "old quoted reply" not in md     # quote chain dropped
    assert "pixel.gif" not in md            # tracking pixel dropped
    assert "Invoice ready" in md and "€42" in md
    assert "| ---" not in md                # layout table flattened, no md table
    assert "(https://shop.example.com/order/9)" in md   # tracker unwrapped
    assert "utm_source=nl" not in md        # utm stripped from clean link
    assert "https://real.example.com/page" in md
    assert "Jane Doe" in sig and "Jane Doe" not in md   # signature split


def test_clean_html_strips_tracking_params_and_collapses_monsters():
    fake = FakeAny()
    mod = load(gmail_fx({}), fake)
    long_tail = "&otpToken=" + "Z" * 400
    html = ('<p><a href="https://www.linkedin.com/feed/update/urn:li:activity:74917'
            '?origin=NET&lipi=urn%3Ali%3Apage&midToken=AQH&midSig=161&trk=eml-card'
            '&trkEmail=eml-null&eid=ub3wz">post</a>'
            '<a href="https://ex.com/doc?utm_source=x&gclid=1&keep=1">doc</a>'
            f'<a href="https://ex.com/monster?body={"Q" * 400}{long_tail}">huge</a></p>')
    md = mod.clean_html(html)["markdown"]
    assert "midToken" not in md and "otpToken" not in md and "lipi" not in md
    assert "(https://www.linkedin.com/feed/update/urn:li:activity:74917)" in md
    assert "(https://ex.com/doc?keep=1)" in md      # non-tracking param kept
    assert "monster" not in md and "huge" in md     # collapsed to its text


def test_clean_html_unwraps_image_only_anchors():
    # an anchor whose whole content is a (stripped) image used to render
    # as [[ / [](…) artifacts
    fake = FakeAny()
    mod = load(gmail_fx({}), fake)
    md = mod.clean_html('<a href="https://x.com/p"><img src="a.png" alt="pic"></a>'
                        '<p>body</p>')["markdown"]
    assert "[" not in md and "body" in md


def test_clean_html_footer_trim_cuts_notification_tail():
    fake = FakeAny()
    mod = load(gmail_fx({}), fake)
    html = "<p>PR #7 merged</p><p>You are receiving this because you commented.</p>"
    out = mod.clean_html(html)
    assert "PR #7 merged" in out["markdown"]
    assert "receiving this" not in out["markdown"]


# --- full-sync slice ---------------------------------------------------------

def test_full_slice_creates_skips_and_checkpoints():
    raws = {m: raw_msg(m) for m in ("m1", "m2", "m3")}
    existing = {"m2": {"id": "old2", "email": {"gmail_id": "m2"}}}
    fake = FakeAny(emails=existing)
    fx = gmail_fx(raws, pages=[
        {"messages": ["m1", "m2"], "nextPageToken": "P2"},
        {"messages": ["m3"]}])
    out = load(fx, fake).sync_now("sp")
    assert out["mode"] == "full" and out["done"] is True
    assert out["made"] == 2 and out["skipped"] == 1 and out["failed"] == 0
    st = fake.state()
    assert st["cursor"] == "H100"           # profile historyId, pre-listing
    assert st["page_token"] == "" and st["synced_count"] == 2
    assert len(fake.markdowns) == 2         # bodies landed as markdown
    # ensure-resolve ran (§5 provisioning contract)
    assert fake.types_created == ["email", "sync_state"]


def test_tick_refuses_cleanly_when_run_budget_already_spent():
    # a conversation run can arrive nearly dry (seen live: two runs
    # died at the 50B wall) — entry check refuses before any write
    fake = FakeAny()
    out = load(gmail_fx({}, fuel=[1_000_000_000] * 3), fake).sync_now("sp")
    assert out == {"mode": "none", "fuelStop": True, "made": 0,
                   "done": False, "note": out["note"]}
    assert "cron" in out["note"]
    assert fake.states == []                # nothing touched


def test_full_slice_fuel_governor_checkpoints_mid_chunk():
    ids = [f"m{i}" for i in range(30)]
    raws = {m: raw_msg(m) for m in ids}
    fake = FakeAny()
    # checks: entry, list page, pre-chunk, then one per message — fuel
    # dries up after 10 messages, the chunk breaks mid-way
    big, low = 50_000_000_000, 1_000_000_000
    fx = gmail_fx(raws, pages=[{"messages": ids}],
                  fuel=[big] * 13 + [low] * 10)
    out = load(fx, fake).sync_now("sp")
    assert out.get("fuelStop") is True and out["done"] is False
    assert out["made"] == 10                # stopped between messages
    assert fake.state()["synced_count"] == 10   # checkpointed before exit


# --- incremental tick --------------------------------------------------------

def test_incremental_coalesces_and_applies():
    raws = {"m9": raw_msg("m9"), "m1": raw_msg("m1", labels=("INBOX", "STARRED"))}
    fake = FakeAny(emails={
        "m1": {"id": "o1", "email": {"gmail_id": "m1", "label_ids": ["INBOX"]}},
        "m2": {"id": "o2", "email": {"gmail_id": "m2"}}},
        states=[{"id": "st1", "modifiedAt": 5,
                 "sync_state": {"cursor": "H100", "page_token": "",
                                "synced_count": 2}}])
    history = {"historyId": "H200", "history": [
        {"messagesAdded": [{"message": {"id": "m9"}}]},
        {"labelsAdded": [{"message": {"id": "m1"}}]},
        {"messagesDeleted": [{"message": {"id": "m2"}}]},
        {"messagesAdded": [{"message": {"id": "mX"}}]},   # add+delete = noop
        {"messagesDeleted": [{"message": {"id": "mX"}}]},
    ]}
    # the scope oracle: m9 is in the q's scope
    out = load(gmail_fx(raws, pages=[{"messages": ["m9", "m1"]}],
                        history=history), fake).sync_now("sp")
    assert out["mode"] == "incremental" and out["done"] is True
    assert out["made"] == 1 and out["deleted"] == 1 and out["labels"] == 1
    assert "o2" in fake.deleted             # hard delete applied
    assert "m9" in fake.emails              # add hydrated format=full
    label_update = next(b for oid, b in fake.updates
                        if oid == "o1" and "email" in b)
    assert label_update["email"]["label_ids"] == ["INBOX", "STARRED"]
    st = fake.state()
    assert st["cursor"] == "H200" and st["synced_count"] == 2  # +1 -1


def test_incremental_add_outside_scope_is_skipped():
    # history.list is scope-blind: an excluded sender's arrival shows up
    # as messagesAdded — the scoped-list oracle keeps it out (the live
    # LinkedIn leak, 2026-08-13)
    raws = {"mLI": raw_msg("mLI")}
    fake = FakeAny(states=[{"id": "st1", "modifiedAt": 5,
                            "sync_state": {"cursor": "H100", "page_token": "",
                                           "synced_count": 3}}])
    history = {"historyId": "H200", "history": [
        {"messagesAdded": [{"message": {"id": "mLI"}}]}]}
    out = load(gmail_fx(raws, pages=[{"messages": ["other1", "other2"]}],
                        history=history), fake).sync_now("sp")
    assert out["made"] == 0 and out["outOfScope"] == 1
    assert "mLI" not in fake.emails
    assert fake.state()["cursor"] == "H200"   # cursor still advances


def test_incremental_cursor_404_resets_to_full_relist():
    fake = FakeAny(states=[{"id": "st1", "modifiedAt": 5,
                            "sync_state": {"cursor": "H1", "page_token": "",
                                           "synced_count": 7}}])
    out = load(gmail_fx({}, history_status=404), fake).sync_now("sp")
    assert "fallback" in out and out["done"] is False
    st = fake.state()
    assert st["cursor"] == "" and st["synced_count"] == 7   # count kept


# --- sync_state dance --------------------------------------------------------

def test_duplicate_sync_states_freshest_wins_stale_deleted():
    fake = FakeAny(states=[
        {"id": "stOld", "modifiedAt": 5,
         "sync_state": {"cursor": "H1", "page_token": "", "synced_count": 1}},
        {"id": "stNew", "modifiedAt": 9,
         "sync_state": {"cursor": "H100", "page_token": "", "synced_count": 4}}],
    )
    history = {"historyId": "H101", "history": []}
    out = load(gmail_fx({}, history=history), fake).sync_now("sp")
    assert "stOld" in fake.deleted
    assert out["cursor"] == "H101"
    assert fake.states[0]["id"] == "stNew"  # survivor carried the tick


# --- status ------------------------------------------------------------------

def test_status_reports_state_and_counts():
    fake = FakeAny(
        emails={"m1": {"id": "o1", "email": {"gmail_id": "m1"}}},
        states=[{"id": "st1", "modifiedAt": 5,
                 "sync_state": {"cursor": "H9", "page_token": "PT",
                                "synced_count": 1}}])
    mod = load(gmail_fx({}), fake)
    out = mod.status("sp")
    assert out == {"configured": True, "cursor": "H9", "pageToken": "PT",
                   "syncedCount": 1, "emailCount": 1}
