"""programs/brainKit@v1 — the brain's shared helpers, under the REAL guest
kernel with the REAL any@v1 over faked HTTP routes. Pinned: the mailbox
type exists only once a mail sync has run, so a space without it has no
mailboxes, and the brain reads that as none rather than failing."""

import json

from kernelenv import load_kernel

SPACE = "bafyreispace.0000000000000"
PAGE = {"id": "type-page", "xKey": "page", "name": "Page", "builtIn": True}
MAILBOX = {"id": "type-mailbox", "xKey": "mailbox", "name": "Mailbox"}
ADDRESS = {"id": "prop-address", "key": "address", "name": "address", "format": "text"}


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
    return load_kernel(effect=effect).use("brainKit@v1")


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
