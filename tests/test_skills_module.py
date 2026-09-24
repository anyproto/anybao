"""skills@v1 — the bound get_skill(name) global (ADR-009 §3): the body of
an on-demand skill by name, looked up in the bao space, then the agent
overlay, then the connectors overlay."""

import pytest
from kernelenv import load_kernel


class FakeAny:
    def __init__(self, skills):
        self.skills = skills           # space -> [(id, name, body)]
        self.reads = []

    def list_types(self, space):
        return [{"id": "t", "xKey": "agent_skill"}] if space in self.skills else []

    def query_objects(self, space, filter=None, **kw):
        assert filter == {"any.type": "agent_skill"}
        return [{"id": i, "any": {"name": n}} for i, n, _ in self.skills.get(space, [])]

    def get_markdown(self, space, oid):
        self.reads.append((space, oid))
        return next(b for i, _, b in self.skills[space] if i == oid)


def _get_skill(fake, spaces):
    app = load_kernel(effect=lambda n, p: {}, any_client=fake)
    return app.use("skills@v1").binder(spaces)


def test_lookup_order_bao_then_agent_then_connectors():
    fake = FakeAny({"bao": [("b1", "review-pr", "# mine")],
                    "agent": [("a1", "review-pr", "# shipped"), ("a2", "files", "# files")],
                    "conn": [("c1", "files", "# conn files"), ("c2", "crm", "# crm")]})
    get_skill = _get_skill(fake, ["bao", "agent", "conn"])
    assert get_skill("review-pr") == "# mine"          # the user's shadows the shipped
    assert get_skill("files") == "# files"             # agent before connectors
    assert get_skill("crm") == "# crm"                 # connectors last


def test_blank_body_does_not_shadow_and_none_spaces_drop():
    fake = FakeAny({"bao": [("b1", "files", "  \n")], "agent": [("a1", "files", "# files")]})
    get_skill = _get_skill(fake, ["bao", None, "agent", "bao"])
    assert get_skill("files") == "# files"


def test_unknown_name_lists_the_known_ones():
    fake = FakeAny({"agent": [("a1", "files", "# f"), ("a0", "_core", "# c")],
                    "conn": [("c2", "crm", "# crm")]})
    get_skill = _get_skill(fake, ["bao", "agent", "conn"])
    with pytest.raises(Exception, match=r"no skill named 'mail'; known: crm, files"):
        get_skill("mail")
