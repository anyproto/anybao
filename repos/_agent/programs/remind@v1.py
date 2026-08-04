"""Deliver a scheduled reminder into a chat (the once-trigger payload).

Runs as a trigger program (ADR-006 §4): args carry {"space", "chatId",
"text"} written by whoever created the trigger. Posts the reminder as
an agent chat message and returns the send receipt.
"""

# The counterpart recipe (how the agent schedules one) lives in the
# _core skill: upsert an agent_triggers record with kind "once",
# spec {"at": epoch}, program "agent:remind@v1" and these args.

__any_tool__ = False  # trigger-run only; not an agent-callable tool


def main(args):
    c = use("any@v1")  # noqa: F821 - guest global
    text = (args or {}).get("text") or "(reminder with no text)"
    return c.chat_send(args["space"], args["chatId"],
                       {"text": f"⏰ Reminder: {text}",
                        "agent": {"name": "bao", "done": True}})
