"""programs/remind@v1 — the once-trigger reminder payload, exec'd with
a fake any@v1 capturing the chat send."""

import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REMIND_SRC = (ROOT / "repos" / "_agent" / "programs" / "remind@v1.py").read_text()
ANY_SRC = (ROOT / "repos" / "_agent" / "programs" / "any@v1" / "program.py").read_text()


def run_main(args):
    calls = []

    def fx(name, payload):
        if name in ("config.get", "runtime.get"):
            return {"value": "http://any"}
        calls.append((name, payload))
        return {"status": 201, "headers": {},
                "body": json.dumps({"recordIds": ["m1"]})}

    nospan = lambda name=None, kind=None: (lambda f: f)  # noqa: E731
    any_g = {"effect": fx, "span": nospan, "use": None}
    exec(compile(ANY_SRC, "any@v1.py", "exec"), any_g)
    g = {"effect": fx, "span": nospan,
         "use": lambda spec: {"any@v1": SimpleNamespace(**any_g)}[spec]}
    exec(compile(REMIND_SRC, "remind@v1.py", "exec"), g)
    return g["main"](args), calls


def test_remind_posts_agent_chat_message():
    out, calls = run_main({"space": "s1", "chatId": "chat1",
                           "text": "check the oven"})
    assert out == {"recordIds": ["m1"]}
    (name, payload) = calls[-1]
    assert name == "http.post"
    assert payload["url"].endswith("/v1/spaces/s1/objects/chat1/chat/messages")
    body = payload["json"]
    assert body["text"] == "⏰ Reminder: check the oven"
    assert body["agent"] == {"name": "bao", "done": True}


def test_remind_survives_missing_text():
    out, calls = run_main({"space": "s1", "chatId": "chat1"})
    assert "(reminder with no text)" in calls[-1][1]["json"]["text"]
    assert out["recordIds"] == ["m1"]
