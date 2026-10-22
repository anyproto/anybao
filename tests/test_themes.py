"""ADR-008 §8: the tool runs in the real guest and preserves UI wire names."""

import kernelenv


def test_discover_choose_preview_and_commit_use_one_recorded_boundary():
    calls = []
    window = "d235081b-d19a-4d48-a5f8-c2e3bb6bcf33"
    replies = [
        {"windows": [{"windowId": window, "ok": True, "result": {"active": True}}]},
        {"ok": True, "windowId": window, "state": {"selectedId": "any"},
         "result": {"previewId": "candidate"}},
        {"ok": False, "requestId": "commit", "state": {"preview": {"id": "candidate"}},
         "error": {"code": "storage_failed", "message": "Full"}},
    ]

    def effect(name, payload):
        assert name == "ui.theme"
        calls.append(payload)
        return replies[len(calls) - 1]

    tool = kernelenv.load_kernel(effect=effect).use("themes@v1")
    found = tool.discover()
    chosen = found["windows"][0]["windowId"]
    preview = tool.request("preview", chosen, {"file": {"spaceId": "sp", "fileId": "f"}})
    kept = tool.request("commit", chosen, {"previewId": preview["result"]["previewId"]})
    assert kept == replies[2]  # a failed Keep remains an explicit UI result
    assert calls == [
        {"action": "capabilities", "timeout": 1},
        {"action": "preview", "windowId": window, "timeout": 5,
         "params": {"file": {"spaceId": "sp", "fileId": "f"}}},
        {"action": "commit", "windowId": window, "timeout": 5,
         "params": {"previewId": "candidate"}},
    ]


def test_unknown_outcome_is_returned_once_without_retry():
    calls = []
    failure = {"ok": False, "requestId": "lost",
               "error": {"code": "outcome_unknown", "message": "Read state first"}}

    def effect(name, payload):
        assert name == "ui.theme"
        calls.append(payload)
        return failure

    tool = kernelenv.load_kernel(effect=effect).use("themes@v1")
    assert tool.request("select", "window", {"themeId": "mint"}, timeout=3) == failure
    assert len(calls) == 1
