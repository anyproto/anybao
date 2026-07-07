import pytest
from anybao.config import Config, ConfigError, DictConfigStore, register_config_effect


def test_cascade_local_over_space_over_default():
    store = DictConfigStore({
        "a": {"value": "space", "localValue": "device"},
        "b": {"value": "space"},
    })
    c = Config(store)
    c.define("c", default="dflt")
    assert c.get("a") == "device"   # localValue wins
    assert c.get("b") == "space"    # synced value
    assert c.get("c") == "dflt"     # default


def test_missing_key_raises():
    with pytest.raises(ConfigError):
        Config(DictConfigStore()).get("nope")


def test_secret_refuses_synced_write():
    c = Config(DictConfigStore())
    c.define("llm.key", secret=True)
    with pytest.raises(ConfigError):
        c.set("llm.key", "sk-123", scope="space")
    c.set("llm.key", "sk-123", scope="device")   # device ok
    assert c.get("llm.key") == "sk-123"


def test_config_effect_hides_secrets_from_cells():
    from anyrt import trace as tr
    from anyrt.effects import Broker, Registry

    c = Config(DictConfigStore({"loop.max_turns": {"value": 50}}))
    c.define("llm.key", secret=True)
    c.set("llm.key", "sk", scope="device")
    reg = Registry()
    register_config_effect(reg, c)
    b = Broker(reg, tr.TraceWriter(run={"id": "c"}))
    assert b.call("config.get", {"key": "loop.max_turns"})["value"] == 50
    with pytest.raises(Exception, match="secret"):
        b.call("config.get", {"key": "llm.key"})
