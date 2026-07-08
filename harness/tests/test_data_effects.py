from anybao.data_effects import register_data_effects
from anyrt import trace as tr
from anyrt.effects import Broker, Registry

READS = ["any.query", "any.query_objects", "any.search", "any.aggregate",
         "any.get_markdown", "any.list_properties"]
WRITES = ["any.modify", "any.create_object", "any.create_type",
          "any.add_property", "any.upsert_record"]


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **kw):
            self.calls.append((name, a, kw))
            return {"ok": name}
        return call


def _broker():
    fc = FakeClient()
    reg = Registry()
    register_data_effects(reg, fc)
    w = tr.TraceWriter(run={"id": "r"})
    return fc, reg, Broker(reg, w), w


def test_read_vs_mutate_classification_and_caps():
    _, reg, _, _ = _broker()
    for name in READS:
        assert reg.get(name).kind == "read" and reg.get(name).cap == "data.read"
    for name in WRITES:
        assert reg.get(name).kind == "mutate" and reg.get(name).cap == "data.write"


def test_query_drops_absent_options():
    fc, _, b, _ = _broker()
    b.call("any.query", {"space": "s1", "object_id": "o1", "dataset": "chat_messages",
                         "limit": 5})
    name, args, kw = fc.calls[0]
    assert name == "query"
    assert args == ("s1", "o1", "chat_messages")
    assert kw == {"limit": 5}  # filter/sort/offset absent, not None


def test_write_traces_as_mutate_effect():
    fc, _, b, w = _broker()
    b.call("any.create_object", {"space": "s1", "body": {"name": "x"}})
    assert fc.calls[0] == ("create_object", ("s1", {"name": "x"}), {})
    rec = next(r for r in w.records if r["kind"] == "effect")
    assert rec["effect"] == "any.create_object" and rec["meta"]["class"] == "mutate"


def test_search_passes_keyword_options():
    fc, _, b, _ = _broker()
    b.call("any.search", {"space": "s1", "query": "hello", "limit": 3})
    name, args, kw = fc.calls[0]
    assert name == "search" and args == ("s1", "hello")
    assert kw == {"scopes": None, "limit": 3, "mode": None}
