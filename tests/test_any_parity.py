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


# --- definitions -------------------------------------------------------------------------

def test_patch_type_and_patch_collection_send_only_what_changes():
    cat = {**_CAT, "/collections": {"collections": [
        {"id": "bafyTAG", "name": "Reading list", "xKey": "reading_list"}]}}
    fx = wire(replies=cat)
    c = client(fx)
    assert c.patch_type("s1", "task", name="Job") == {"id": "bafyTASK", "name": "Job"}
    c.patch_collection("s1", "reading_list", description="", meta={"pinned": True})
    writes = [x for x in fx.calls if x[0] == "PATCH"]
    assert writes == [("PATCH", "/v1/spaces/s1/types/bafyTASK", {"name": "Job"}),
                      ("PATCH", "/v1/spaces/s1/collections/bafyTAG",
                       {"description": "", "meta": {"pinned": True}})]
    # the rename is read back: the catalog cache is dropped
    n = len([1 for v, p, _ in fx.calls if p.endswith("/types")])
    c.get_type("s1", "task")
    assert len([1 for v, p, _ in fx.calls if p.endswith("/types")]) == n + 1
    try:
        c.patch_type("s1", "task")
    except ValueError as e:
        assert "nothing to change" in str(e)
    else:
        raise AssertionError("an empty patch must raise")


def test_uninstall_bundle_is_unlisted_and_encodes_the_id():
    fx = wire()
    c = client(fx)
    assert c.uninstall_bundle("s1", "system:notes/v1") == {}
    assert fx.calls == [("DELETE", "/v1/spaces/s1/bundles/system%3Anotes%2Fv1", None)]
    assert c.uninstall_bundle.__any_public__[2] is False


# --- spaces ------------------------------------------------------------------------------------

def test_query_spaces_reads_every_status_and_trims_the_key_material():
    fx = wire(replies={"/spaces/query": {"records": [
        {"id": SID, "name": "x", "localStatus": "one_to_one_pending",
         "push": {"spaceKey": "secret"}, "_ver": {"id": "!"}}]}})
    r = client(fx).query_spaces(filter={"localStatus": "one_to_one_pending"}, limit=None)
    assert r == [{"id": SID, "name": "x", "localStatus": "one_to_one_pending"}]
    assert _one(fx) == ("POST", "/v1/spaces/query",
                        {"filter": {"localStatus": "one_to_one_pending"}})


def test_list_spaces_by_status_reads_the_pending_rows_and_keeps_the_name_cache():
    fx = wire(replies={"/v1/spaces": {"spaces": [
        {"id": SID, "name": "x", "status": "invite_pending", "push": {"k": 1}}]}})
    c = client(fx)
    assert c.list_spaces(status="invite_pending") == [
        {"id": SID, "name": "x", "status": "invite_pending"}]
    assert fx.urls == ["/v1/spaces?status=invite_pending"]
    assert c._spaces_cache is None          # a pending row is not a name to resolve


def test_patch_space_sends_only_what_changes_and_drops_the_name_cache():
    fx = wire(replies={"/v1/spaces": {"spaces": [{"id": SID, "name": "old"}]}})
    c = client(fx)
    c._spaces()                                      # the name catalog is cached
    c.patch_space("s1", name="New", description="")
    assert fx.calls[-1] == ("PATCH", "/v1/spaces/s1", {"name": "New", "description": ""})
    assert c._spaces_cache is None
    try:
        c.patch_space("s1")
    except ValueError as e:
        assert "needs" in str(e)
    else:
        raise AssertionError("an empty patch must raise")


def test_the_direct_space_calls():
    fx = wire(replies={"": {"id": SID, "name": "", "status": "active",
                            "spaceType": "any.onetoone", "push": {"k": 1}}})
    c = client(fx)
    row = c.open_direct_space("A2")
    assert row["spaceType"] == "any.onetoone" and "push" not in row
    c.register_incoming_direct("A2", {"name": "Bo"})
    assert c.answer_direct_space(SID, True)["id"] == SID
    c.answer_direct_space(SID, False)
    assert fx.calls == [
        ("POST", "/v1/spaces/one-to-one", {"otherIdentity": "A2"}),
        ("POST", "/v1/spaces/one-to-one/register-incoming",
         {"peerIdentity": "A2", "displayHint": {"name": "Bo"}}),
        ("POST", f"/v1/spaces/{SID}/one-to-one/accept", None),
        ("POST", f"/v1/spaces/{SID}/one-to-one/decline", None)]


# --- sharing -----------------------------------------------------------------------------------

SP = "/v1/spaces/s1"


def test_invites_and_the_guest_key():
    fx = wire(replies={"/invites/r1": {"recordId": "r1", "permission": "none"},
                       "/invites": {"invites": [{"recordId": "r1"}], "inviteToken": "T"},
                       "/guest-key": {"inviteToken": "G"}})
    c = client(fx)
    assert c.list_invites("s1") == [{"recordId": "r1"}]
    assert c.list_invites("s1", "r1") == [{"recordId": "r1", "permission": "none"}]
    assert c.create_invite("s1")["inviteToken"] == "T"
    c.revoke_invite("s1", "r1")
    c.revoke_invite("s1")
    assert c.create_guest_key("s1") == {"inviteToken": "G"}
    c.revoke_guest_key("s1")
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", SP + "/invites"), ("GET", SP + "/invites/r1"), ("POST", SP + "/invites"),
        ("DELETE", SP + "/invites/r1"), ("DELETE", SP + "/invites"),
        ("POST", SP + "/guest-key"), ("DELETE", SP + "/guest-key")]


def test_join_requests_are_accepted_by_identity_or_declined():
    fx = wire(replies={"/members/requests": {"requests": [
        {"recordId": "q1", "identity": "A2", "name": "Bo"}]}})
    c = client(fx)
    c.answer_join_request("s1", "A2", "writer")
    c.answer_join_request("s1", "A2")
    posts = [(p, b) for v, p, b in fx.calls if v == "POST"]
    assert posts == [(SP + "/acl/accept", {"requestRecordId": "q1", "permission": "writer"}),
                     (SP + "/acl/decline", {"identity": "A2"})]
    try:
        c.answer_join_request("s1", "A9", "reader")
    except ValueError as e:
        assert "no pending join request" in str(e)
    else:
        raise AssertionError("an unknown requester must raise")


def test_the_access_control_writes_and_the_roster_cache():
    fx = wire(replies={"/members": {"members": [{"identity": "A1", "status": "active"}]}})
    c = client(fx)
    c.list_members("s1")
    c.add_members("s1", [{"identity": "A2", "permission": "writer", "name": "Bo"}])
    assert "s1" not in c._members_cache            # the roster is read again
    c.set_member_permissions("s1", {"A2": "reader"})
    c.remove_members("s1", "A2")
    c.transfer_ownership("s1", "A3")
    c.leave_space("s1")
    c.stop_sharing("s1")
    posts = [(p, b) for v, p, b in fx.calls if v == "POST"]
    assert posts == [
        (SP + "/acl/add", {"accounts": [{"identity": "A2", "permission": "writer",
                                         "metadata": {"name": "Bo"}}]}),
        (SP + "/acl/permissions", {"changes": [{"identity": "A2", "permission": "reader"}]}),
        (SP + "/acl/remove", {"identities": ["A2"]}),
        (SP + "/acl/ownership", {"newOwner": "A3", "oldOwnerPerm": "admin"}),
        (SP + "/acl/self-remove", None), (SP + "/acl/stop-sharing", None)]


def test_join_cancel_and_answering_a_space_invite():
    fx = wire(replies={"": {"id": SID, "status": "joining", "push": {"k": 1}}})
    c = client(fx)
    assert c.join_space("TOKEN", name="Me") == {"id": SID, "status": "joining"}
    c.cancel_join(SID)
    c.answer_space_invite(SID, True)
    c.answer_space_invite(SID, False)
    assert fx.calls == [
        ("POST", "/v1/spaces/join", {"inviteToken": "TOKEN", "metadata": {"name": "Me"}}),
        ("POST", f"/v1/spaces/{SID}/acl/cancel-join", None),
        ("POST", f"/v1/spaces/{SID}/invite/accept", None),
        ("POST", f"/v1/spaces/{SID}/invite/decline", None)]


def test_every_sharing_write_says_to_ask_the_user_first():
    g = load(wire())
    for name in ("create_invite", "revoke_invite", "create_guest_key", "revoke_guest_key",
                 "join_space", "answer_space_invite", "answer_join_request", "add_members",
                 "set_member_permissions", "remove_members", "transfer_ownership",
                 "leave_space", "stop_sharing"):
        assert "sk the user first" in " ".join(g[name].__doc__.split()), name


NAMES = ("list_changes", "get_version", "get_record_version", "diff_versions",
         "get_object", "get_type", "get_collection", "get_member",
         "account_limits", "account_icon", "identity_icon", "set_account_icon",
         "local_discovery", "set_local_discovery", "list_unread", "set_space_settings",
         "chat_edit", "chat_delete", "chat_react", "chat_mark_read",
         "create_block", "patch_block", "delete_block",
         "query_dataset", "aggregate_dataset",
         "query_files", "file_stats", "file_info", "pin_file", "offload_file", "retry_file",
         "patch_type", "patch_collection", "uninstall_bundle",
         "query_spaces", "patch_space", "open_direct_space",
         "register_incoming_direct", "answer_direct_space",
         "list_invites", "create_invite", "revoke_invite", "create_guest_key",
         "revoke_guest_key", "join_space", "cancel_join", "answer_space_invite",
         "list_join_requests", "answer_join_request", "add_members",
         "set_member_permissions", "remove_members", "transfer_ownership",
         "leave_space", "stop_sharing")
