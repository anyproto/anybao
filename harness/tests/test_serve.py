"""`anybao serve` composition pieces — offline (fake transports)."""

from anybao import cli
from anybao.anyclient import AnyClient


def test_bootstrap_config_env_key_and_tiers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    cfg = cli.bootstrap_config()
    assert cfg.get("llm.key.anthropic") == "sk-x"
    tier = cfg.get("llm.tier.classify")
    assert tier["provider"] == "anthropic"
    assert tier["api_key_ref"] == "llm.key.anthropic"


def test_ensure_space_adopts_active_by_name():
    def send(method, path, body):
        if path.startswith("/v1/spaces") and method == "GET":
            return 200, {"spaces": [
                {"id": "s-old", "name": "bao", "status": "archived"},
                {"id": "s-live", "name": "bao", "status": "active"}]}
        raise AssertionError("should not create")
    assert cli.ensure_space(AnyClient(send), "bao") == "s-live"


def test_ensure_space_creates_when_missing():
    calls = []

    def send(method, path, body):
        calls.append((method, path))
        if method == "GET":
            return 200, {"spaces": []}
        return 200, {"id": "s-new"}
    assert cli.ensure_space(AnyClient(send), "bao") == "s-new"
    assert ("POST", "/v1/spaces") in calls


def test_ensure_chat_finds_chat_typed_object_else_creates():
    def send(method, path, body):
        if path.endswith("/objects/query"):
            return 200, {"records": [
                {"id": "page1", "editor": {}},              # name matches, not a chat
                {"id": "chat1", "chat": {}}]}
        raise AssertionError("should not create")
    assert cli.ensure_chat(AnyClient(send), "s1", "general") == "chat1"

    def send_empty(method, path, body):
        if path.endswith("/objects/query"):
            return 200, {"records": []}
        return 200, {"objectId": "chat-new"}
    assert cli.ensure_chat(AnyClient(send_empty), "s1", "general") == "chat-new"


def test_chat_messages_drops_snapshot_and_yields_added():
    frames = [
        {"event": "ready", "data": {}},
        {"event": "snapshot", "data": [{"added": [{"id": "old", "doc": {"text": "old"}}]}]},
        {"event": "changes", "data": [{"added": [{"id": "m1", "doc": {"text": "hi"}}]}]},
        {"event": "closed", "data": {"reason": "bye"}},
    ]

    class FakeClient:
        def subscribe_dataset(self, space, obj, dataset, **opts):
            assert dataset == "chat_messages"
            yield from frames

    got = list(cli.chat_messages(FakeClient(), "s1", "chat1"))
    assert got == [{"id": "m1", "text": "hi"}]
