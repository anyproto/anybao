"""The any API parity helpers of programs/any@v1 (docs/api-parity.md
§C10): the wire shape of each call, against the fake wire."""

import json

from test_any_module import _CAT, REF, SID, client, load, wire

OBJ = "/v1/spaces/s1/objects/o1"


def _one(fx):
    """The single call a helper made → (VERB, url-with-query, body)."""
    (verb, _, body), = fx.calls
    return verb, fx.urls[0], body


def test_the_parity_helpers_are_on_the_flat_surface_with_docs():
    g = load(wire())
    for name in NAMES:
        assert callable(g[name]) and g[name].__doc__, name


# --- object history ------------------------------------------------------------

def test_list_changes_pages_and_filters():
    fx = wire(replies={"/history": {"changes": [{"version": "v2"}], "cursor": ""}})
    r = client(fx).list_changes("s1", "o1", limit=5, author="A1",
                                dataset="editor_blocks", grouped=True)
    assert r == {"changes": [{"version": "v2"}], "cursor": None}
    assert _one(fx) == ("GET", OBJ + "/history?limit=5&author=A1"
                        "&dataset=editor_blocks&coalesce=true", None)


def test_get_version_reads_the_object_row_like_a_query_row():
    fx = wire(replies={**_CAT, "/history/v1": {"version": "v1", "datasets": [
        {"dataset": "objects", "records": [
            {"id": "o1", "_ver": {"id": "!"}, "any": {"name": "Ship", "type": "bafyTASK"},
             "bafyTASK": {"bafySTATUS": "open"}}]},
        {"dataset": "editor_blocks", "records": [
            {"id": "b1", "_ver": {"id": "!"}, "_addSeq": 3, "text": "hi"}]}]}})
    r = client(fx).get_version("s1", "o1", "v1")
    objects, blocks = r["datasets"]
    assert objects["records"] == [{"id": "o1", "any": {"name": "Ship", "type": "task"},
                                   "task": {"status": "open"}}]
    assert blocks["records"] == [{"id": "b1", "text": "hi"}]
    assert fx.urls[0] == OBJ + "/history/v1"


def test_get_record_version_takes_the_fast_path():
    fx = wire(replies={"/records/b%3A1": {"exists": True, "record": {
        "id": "b:1", "_ver": {}, "text": "hi"}}})
    r = client(fx).get_record_version("s1", "o1", "v1", "editor_blocks", "b:1")
    assert r == {"exists": True, "record": {"id": "b:1", "text": "hi"}}
    assert fx.urls[0] == OBJ + "/history/v1/datasets/editor_blocks/records/b%3A1"


def test_diff_versions_names_the_property_paths():
    fx = wire(replies={**_CAT, "/history/diff": {"base": "", "version": "v2", "datasets": [
        {"dataset": "objects", "records": [{"id": "o1", "kind": "changed", "fields": [
            {"path": ["any", "name"], "before": "A", "after": "B"},
            {"path": ["bafyTASK", "bafySTATUS"], "after": "done"}]}]},
        {"dataset": "editor_blocks", "records": [{"id": "b1", "kind": "changed",
                                                  "fields": [{"path": ["text"]}]}]}]}})
    r = client(fx).diff_versions("s1", "o1", "v2", base="v1")
    paths = [f["path"] for f in r["datasets"][0]["records"][0]["fields"]]
    assert paths == [["any", "name"], ["task", "status"]]
    assert r["datasets"][1]["records"][0]["fields"] == [{"path": ["text"]}]
    assert fx.urls[0] == OBJ + "/history/diff?version=v2&base=v1"


# --- single reads --------------------------------------------------------------

def test_get_object_reads_one_row_like_a_query_row():
    fx = wire(replies={**_CAT, "/objects/o1": {"objectId": "o1", "record": {
        "id": "o1", "_ver": {"id": "!"}, "any": {"name": "Ship", "type": "bafyTASK"},
        "bafyTASK": {"bafySTATUS": "open"}}}})
    c = client(fx)
    r = c.get_object("s1", "o1")
    assert r == {"id": "o1", "any": {"name": "Ship", "type": "task"},
                 "task": {"status": "open"}}
    assert ("GET", OBJ, None) in fx.calls
    assert c.get_object("s1", "o1", normalize=False)["_ver"] == {"id": "!"}


def test_get_object_is_none_for_a_missing_object():
    fx = wire(status=404, replies={"": {"error": {"code": "object.not_found"}}})
    assert client(fx).get_object("s1", "gone") is None


def test_get_type_and_get_collection_read_one_definition():
    cat = {**_CAT, "/collections": {"collections": [
        {"id": "bafyTAG", "name": "Reading list", "xKey": "reading_list"}]},
        "/types/bafyTASK": {"id": "bafyTASK", "xKey": "task", "name": "Task"},
        "/collections/bafyTAG": {"id": "bafyTAG", "xKey": "reading_list"}}
    fx = wire(replies=cat)
    c = client(fx)
    assert c.get_type("s1", "task")["name"] == "Task"
    assert c.get_collection("s1", "reading_list")["xKey"] == "reading_list"
    gets = [p for v, p, _ in fx.calls]
    assert "/v1/spaces/s1/types/bafyTASK" in gets
    assert "/v1/spaces/s1/collections/bafyTAG" in gets


def test_get_member_reads_me_or_one_identity():
    fx = wire(replies={"/members/me": {"identity": "A1", "permission": "owner",
                                       "status": "active"},
                       "/members/A2": {"identity": "A2", "name": "Bo",
                                       "permission": "reader", "status": "active"}})
    c = client(fx)
    assert c.get_member("s1")["permission"] == "owner"
    assert c.get_member("s1", "A2") == {"identity": "A2", "name": "Bo",
                                        "permission": "reader", "status": "active"}
    assert [p for _, p, _ in fx.calls] == ["/v1/spaces/s1/members/me",
                                           "/v1/spaces/s1/members/A2"]


# --- account, device, private space settings ------------------------------------

def test_account_limits_and_local_discovery_are_plain_reads_and_one_write():
    fx = wire(replies={"/account/limits": {"storage": {"limitBytes": 0}},
                       "/local-discovery": {"enabled": False}})
    c = client(fx)
    assert c.account_limits() == {"storage": {"limitBytes": 0}}
    assert c.local_discovery() == {"enabled": False}
    c.set_local_discovery(0)
    assert fx.calls == [("GET", "/v1/account/limits", None),
                        ("GET", "/v1/local-discovery", None),
                        ("PUT", "/v1/local-discovery", {"enabled": False})]


def test_icons_come_back_as_blobs_and_none_when_absent():
    def fx(name, payload):
        if payload["url"].endswith("/account/icon"):
            return {"status": 200, "headers": {"content-type": "image/png"},
                    "body": dict(REF)}
        return {"status": 404, "headers": {}, "body": json.dumps(
            {"error": {"code": "identity.icon_not_found"}})}
    g = load(fx)
    c = g["_Client"]("http://any")
    r = c.account_icon()
    assert r["mime"] == "image/png" and isinstance(r["blob"], g["Blob"])
    assert c.identity_icon("A1") is None


def test_set_account_icon_sends_the_image_raw_and_none_removes_it():
    seen = []

    def fx(name, payload):
        seen.append((name, payload))
        return {"status": 200, "headers": {}, "body": json.dumps({"iconCid": "bafyI"})}
    g = load(fx)
    c = g["_Client"]("http://any")
    b = g["Blob"].from_ref(REF)
    assert c.set_account_icon(b, name="me 1.png") == {"iconCid": "bafyI"}
    name, payload = seen[-1]
    assert name == "http.put" and payload["body"] is b
    assert payload["url"] == "http://any/v1/account/icon?name=me%201.png"
    c.set_account_icon(None)
    assert seen[-1][0] == "http.delete" and "body" not in seen[-1][1]


def test_list_unread_pages_by_cursor():
    fx = wire(replies={"/read-state/unread": {"changes": [], "cursor": "c9"}})
    assert client(fx).list_unread("s1", cursor="c1", limit=5) == {"changes": [], "cursor": "c9"}
    assert fx.urls == ["/v1/spaces/s1/read-state/unread?cursor=c1&limit=5"]


def test_set_space_settings_patches_per_key():
    fx = wire()
    c = client(fx)
    c.set_space_settings("s1", set={"notifyMode": "mentions"}, unset=["old"])
    assert _one(fx) == ("PATCH", "/v1/spaces/s1/settings",
                        {"set": {"notifyMode": "mentions"}, "unset": ["old"]})
    try:
        c.set_space_settings("s1")
    except ValueError as e:
        assert "needs" in str(e)
    else:
        raise AssertionError("an empty patch must raise")


# --- chat messages ---------------------------------------------------------------

MSG = OBJ + "/chat/messages/m1"


def test_chat_edit_delete_and_react_hit_the_message_routes():
    fx = wire(replies={"": {"changeId": "c1", "recordIds": ["m1"]}})
    c = client(fx)
    assert c.chat_edit("s1", "o1", "m1", "new text")["recordIds"] == ["m1"]
    c.chat_react("s1", "o1", "m1", "👍")
    c.chat_delete("s1", "o1", "m1")
    assert fx.calls == [("PATCH", MSG, {"text": "new text"}),
                        ("POST", MSG + "/reactions/%F0%9F%91%8D", None),
                        ("DELETE", MSG, None)]


def test_chat_mark_read_picks_the_route_by_its_arguments():
    fx = wire()
    c = client(fx)
    c.chat_mark_read("s1", "o1")
    c.chat_mark_read("s1", "o1", "m1")
    c.chat_mark_read("s1", "o1", "m1", reactions=True)
    assert [p for _, p, _ in fx.calls] == [OBJ + "/chat/read-all", MSG + "/read",
                                           MSG + "/reactions-read"]
    try:
        c.chat_mark_read("s1", "o1", reactions=True)
    except ValueError as e:
        assert "message_id" in str(e)
    else:
        raise AssertionError("reactions without a message must raise")


# --- editor blocks -----------------------------------------------------------------

BLOCKS = OBJ + "/editor/editor_blocks/blocks"
_PAGE = {"/objects/query": {"records": [{"id": "o1", "any": {"type": "page"}}]}}


def test_create_block_posts_one_block_after_the_body_gate():
    fx = wire(replies={**_PAGE, "/blocks": {"recordIds": ["b9"]}})
    r = client(fx).create_block("s1", "o1", {"type": "heading", "text": "Plan",
                                             "style": {"level": 2}})
    assert r["recordIds"] == ["b9"]
    assert fx.calls[-1] == ("POST", BLOCKS, {"type": "heading", "text": "Plan",
                                             "style": {"level": 2}})


def test_patch_and_delete_block_address_one_block():
    fx = wire(replies={"": {"recordIds": ["b:1"]}})
    c = client(fx)
    c.patch_block("s1", "o1", "b:1", set={"style.checked": True}, unset=["style.lang"])
    c.delete_block("s1", "o1", "b:1")
    assert fx.calls == [("PATCH", BLOCKS + "/b%3A1", {"set": {"style.checked": True},
                                                      "unset": ["style.lang"]}),
                        ("DELETE", BLOCKS + "/b%3A1", None)]
    try:
        c.patch_block("s1", "o1", "b:1")
    except ValueError as e:
        assert "needs" in str(e)
    else:
        raise AssertionError("an empty patch must raise")


# --- datasets ------------------------------------------------------------------------

_SHARED = {**_CAT, "/types/bafyTASK/datasets": {"datasets": [
    {"id": "d1", "key": "events", "collection": "bafyTASK_events", "shared": True},
    {"id": "d2", "key": "notes", "collection": "bafyTASK_notes"}]}}
DEFS = "/v1/spaces/s1/types/bafyTASK/datasets/d1"


def test_query_and_aggregate_dataset_read_a_shared_store_across_objects():
    fx = wire(replies={**_SHARED, "/datasets/query": {"records": [{"id": "o1/r1"}]},
                       "/datasets/aggregate": {"records": [{"id": "o1", "n": 2}]}})
    c = client(fx)
    assert c.query_dataset("s1", "task", "events", limit=5, sort=None) == [{"id": "o1/r1"}]
    assert fx.calls[-1] == ("POST", "/v1/spaces/s1/datasets/query",
                            {"dataset": "bafyTASK_events", "limit": 5})
    pipe = [{"$group": {"_id": "$_objectId", "n": {"$count": {}}}}]
    assert c.aggregate_dataset("s1", "task", "events", pipe)["records"][0]["n"] == 2
    assert fx.calls[-1] == ("POST", "/v1/spaces/s1/datasets/aggregate",
                            {"dataset": "bafyTASK_events", "pipeline": pipe})


def test_a_dataset_that_is_not_shared_names_the_per_object_read():
    c = client(wire(replies=_SHARED))
    for ds, word in (("notes", "not shared"), ("nope", "declares no dataset")):
        try:
            c.query_dataset("s1", "task", ds)
        except ValueError as e:
            assert word in str(e)
        else:
            raise AssertionError(ds)


def test_the_dataset_declaration_plumbing_is_private_and_hits_its_routes():
    fx = wire(replies={**_CAT, "/indexes": {"indexDefId": "i1"}})
    g = load(fx)
    for name in ("_patch_dataset_field", "_add_dataset_index", "_remove_dataset_index"):
        assert g[name].__doc__ and name[1:] not in g, name
    c = g["_Client"]("http://any")
    c.patch_dataset_field("s1", "task", "d1", "f1", set={"name": "Start"})
    assert c.add_dataset_index("s1", "task", "d1", {"key": "by_start",
                                                    "fields": ["start"]}) == {"indexDefId": "i1"}
    c.remove_dataset_index("s1", "task", "d1", "i1")
    writes = [x for x in fx.calls if x[0] != "GET"]
    assert writes == [("PATCH", DEFS + "/fields/f1", {"set": {"name": "Start"}}),
                      ("POST", DEFS + "/indexes", {"key": "by_start", "fields": ["start"]}),
                      ("DELETE", DEFS + "/indexes/i1", None)]


# --- files -----------------------------------------------------------------------------

FILE = "/v1/spaces/" + SID + "/files/f1"


def test_file_info_merges_the_facts_and_the_backup_state():
    fx = wire(replies={"/files/f1/status": {"fileId": "f1", "state": "inflight",
                                            "cached": True, "attempts": 2},
                       "/files/f1": {"fileId": "f1", "name": "a.png", "size": 9,
                                     "durable": False}})
    r = client(fx).file_info(f"any://f/{SID}/f1")
    assert r == {"fileId": "f1", "name": "a.png", "size": 9, "durable": False,
                 "state": "inflight", "cached": True, "attempts": 2,
                 "uri": f"any://f/{SID}/f1"}
    assert [p for _, p, _ in fx.calls] == [FILE, FILE + "/status"]


def test_pin_offload_and_retry_post_to_the_file():
    fx = wire()
    c = client(fx)
    link = f"any://f/{SID}/f1"
    c.pin_file(link)
    c.offload_file(link)
    c.retry_file(link)
    assert fx.calls == [("POST", FILE + "/pin", None), ("POST", FILE + "/offload", None),
                        ("POST", FILE + "/retry", None)]


def test_file_stats_and_query_files():
    fx = wire(replies={"/files/stats": {"total": 3, "durable": 3},
                       "/files/query": {"records": [{"id": "f1", "size": 9}]}})
    c = client(fx)
    assert c.file_stats("s1") == {"total": 3, "durable": 3}
    assert c.query_files("s1", "o1", sort=["-size"], limit=None) == [{"id": "f1", "size": 9}]
    assert fx.calls[-1] == ("POST", OBJ + "/files/query", {"sort": ["-size"]})


def test_query_files_is_empty_before_the_first_file():
    fx = wire(status=404, replies={"": {"error": {"code": "file.not_found"}}})
    assert client(fx).query_files("s1", "o1") == []


NAMES = ("list_changes", "get_version", "get_record_version", "diff_versions",
         "get_object", "get_type", "get_collection", "get_member",
         "account_limits", "account_icon", "identity_icon", "set_account_icon",
         "local_discovery", "set_local_discovery", "list_unread", "set_space_settings",
         "chat_edit", "chat_delete", "chat_react", "chat_mark_read",
         "create_block", "patch_block", "delete_block",
         "query_dataset", "aggregate_dataset",
         "query_files", "file_stats", "file_info", "pin_file", "offload_file", "retry_file")
