from anyrt import trace as tr
from anyrt.effects import Broker, Registry

from anybao.effects_impl import register_chat_effect, register_http_effects


def test_http_effects_declared_read_vs_mutate():
    reg = Registry()
    register_http_effects(reg)
    assert reg.get("http.get").kind == "read"
    assert reg.get("http.post").kind == "mutate"
    assert reg.get("http.get").redact == ("headers.authorization",)


def test_chat_send_posts_with_agent_and_traces():
    class FakeClient:
        def __init__(self):
            self.sent = []
        def chat_send(self, space, chat_id, body):
            self.sent.append((chat_id, body))
            return {"recordIds": ["m1"]}

    fc = FakeClient()
    reg = Registry()
    register_chat_effect(reg, fc, space="s1", chat_id="chat1", agent_name="bao")
    w = tr.TraceWriter(run={"id": "r"})
    b = Broker(reg, w)
    b.call("chat.send", {"text": "hi", "done": False})
    assert fc.sent[0][0] == "chat1"
    assert fc.sent[0][1] == {"text": "hi", "agent": {"name": "bao", "done": False}}
    # recorded as a mutate effect
    rec = next(r for r in w.records if r["kind"] == "effect")
    assert rec["effect"] == "chat.send" and rec["meta"]["class"] == "mutate"
