"""programs/deepResearch@v1 under the real guest kernel — the provider
row guard (ADR-008 §4): Gemini's wire only, another provider a config
failure returned before any call."""

import pytest
from kernelenv import load_kernel


def _load(prov):
    calls = []

    def effect(name, payload):
        calls.append(name)
        if name == "config.get":
            return {"value": prov}
        if name == "time.now":
            return {"epoch": 0.0}
        pytest.fail(f"unexpected effect {name!r}")

    return load_kernel(effect=effect).use("deepResearch@v1"), calls


@pytest.mark.parametrize("provider", ["openai-compat", "bing"])
def test_a_non_gemini_provider_is_refused_before_any_call(provider):
    dr, calls = _load({"provider": provider, "model": "codex/gpt-5.5",
                       "base_url": "http://127.0.0.1:20123/v1",
                       "api_key_ref": "llm.key.anyai"})
    out = dr.research("space1", "what changed in rust 2027?")
    assert out == {"ok": False, "error": f"search.provider.deepresearch: unknown provider "
                                         f"{provider!r}; supported: gemini"}
    assert "http.post" not in calls
