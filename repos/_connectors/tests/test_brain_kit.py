"""programs/brainKit@v1 — the brain's shared helpers, under the REAL guest
kernel with the REAL any@v1 over faked HTTP routes. Pinned: the mailbox
type exists only once a mail sync has run, so a space without it has no
mailboxes, and the brain reads that as none rather than failing. The
IdentityResolver is the one rule that turns a chat identity into a person
or a verdict: the identity itself on the list first, then any person it
may be; the user's own identity never."""

import json

from brain_fakes import FakeSpace
from connectorenv import connector_kernel

SPACE = "bafyreispace.0000000000000"
PAGE = {"id": "type-page", "xKey": "page", "name": "Page", "builtIn": True}
MAILBOX = {"id": "type-mailbox", "xKey": "mailbox", "name": "Mailbox"}
ADDRESS = {"id": "prop-address", "xKey": "address", "name": "address"}


def server(types, records=()):
    """An `effect` serving any@v1's routes, and the object queries it saw."""
    queries = []

    def effect(name, payload):
        if name == "runtime.get" and payload["key"] == "any.base_url":
            return {"value": "http://any"}
        if not name.startswith("http."):
            raise AssertionError(f"unexpected effect {name}")
        path = payload["url"].removeprefix("http://any")
        if path.startswith(f"/v1/spaces/{SPACE}/types/") and "/properties" in path:
            body = {"properties": [ADDRESS]}
        elif path.startswith(f"/v1/spaces/{SPACE}/types"):
            body = {"types": list(types)}
        elif path.startswith(f"/v1/spaces/{SPACE}/collections"):
            body = {"collections": []}
        elif path == f"/v1/spaces/{SPACE}/objects/query":
            queries.append(payload.get("json"))
            body = {"records": list(records)}
        else:
            raise AssertionError(f"unexpected route {name} {path}")
        return {"status": 200, "headers": {}, "body": json.dumps(body)}

    return effect, queries


def kit(effect):
    return connector_kernel(effect=effect).use("brainKit@v1")


def test_a_space_that_never_synced_mail_has_no_mailboxes():
    effect, queries = server([PAGE])
    assert kit(effect).mailboxes(SPACE) == []
    assert queries == []


def test_each_synced_mailbox_is_listed_with_its_address():
    record = {"id": "mb1", "any": {"type": MAILBOX["id"], "name": "Me@Example.com"},
              MAILBOX["id"]: {ADDRESS["id"]: "Me@Example.com"}}
    effect, queries = server([PAGE, MAILBOX], [record])
    assert kit(effect).mailboxes(SPACE) == [("mb1", "me@example.com")]
    assert len(queries) == 1


def test_the_identity_resolver_keeps_out_by_identity_then_by_person():
    s = FakeSpace()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace")
    s.add_object("bob", "profile", ["person"], name="Robert Stone")
    s.directory = {"ada-id": "Ada Lovelace", "bob-id": "Bob", "me-id": "Me"}
    k = connector_kernel(any_client=s).use("brainKit@v1")
    excl = {"personIds": {"ada"}, "identities": {"bob-id", "me-id"}}
    who = k.IdentityResolver(s.space, excl, me="me-id")
    assert who.kept_out("bob-id") is True                    # the identity itself: no name needed
    assert [c for c in s.calls if c[0] == "get_identity"] == []
    assert who.kept_out("ada-id") is True                    # through the person she may be
    assert who.kept_out("me-id") is False                    # never the user, even when listed
    assert who.kept_out(None) is False and who.kept_out("") is False
    assert who.person("ada-id") == "ada" and who.person("bob-id") is None
    assert who.candidates("ada-id") == ["ada"] and who.candidates(None) == []
    # a space's member list names identities before the directory is asked
    who.names.add_members([{"identity": "x-id", "name": "Robert Stone"}])
    assert who.person("x-id") == "bob"
    assert [c for c in s.calls if c == ("get_identity", "x-id")] == []


def test_the_identity_resolver_reads_nothing_while_nobody_is_excluded():
    s = FakeSpace()
    s.add_object("ada", "profile", ["person"], name="Ada Lovelace")
    k = connector_kernel(any_client=s).use("brainKit@v1")
    who = k.IdentityResolver(s.space, {"personIds": set(), "identities": set()})
    assert who.kept_out("ada-id") is False
    assert s.calls == []                                     # no name lookup, no person read
