"""What to know before you meet or answer someone: the brain's card for one person.

`person(id)` returns the card: how they want to be addressed, their facts
with the sentence each came from, what you promised them, what they asked
you for, and your latest mail with them, plus the card as markdown. Quote a
fact's evidence when you use it, and say a proposed fact is unconfirmed.
"""

# ADR-035 §4. Deterministic: no model. Built on agent:brainRecall@v1.person,
# which already honours the exclusion list and the blocklist.

__any_tool__ = True  # agent-callable (ADR-010 §4)

recall = use("brainRecall@v1")  # noqa: F821 - guest global

GROUP_TITLES = (("tastes", "Tastes"), ("views", "Views"), ("preferences", "Preferences"),
                ("personal", "Personal"), ("work", "Work"), ("history", "History"))
THREADS = 3


def _first(facts, kind):
    for f in facts:
        if f.get("kind") == kind:
            return f.get("value")
    return None


def _day(seconds):
    return fmt_ts(seconds, "%Y-%m-%d", 0)[:10] if seconds else ""  # noqa: F821


def render(card):
    """The card as markdown for chat."""
    p = card["person"]
    lines = [f"## {p.get('name') or ', '.join(p.get('emails') or [])}"]
    addressing = [x for x in (p.get("addressForm"), p.get("language")) if x]
    if addressing:
        lines.append(f"Address: {' · '.join(addressing)}")
    if p.get("lastContactAt"):
        lines.append(f"Last contact: {_day(p['lastContactAt'])}")
    for key, title in GROUP_TITLES:
        facts = card["facts"].get(key) or []
        if not facts:
            continue
        lines.append(f"\n### {title}")
        lines += [f"- {f['value']} — “{f['quote']}” ({f['source']})" for f in facts]
    if card["proposed"]:
        lines.append("\n### Unconfirmed")
        lines += [f"- {f['value']} — “{f['quote']}”" for f in card["proposed"]]
    if card["promises"]["mine"]:
        lines.append("\n### You promised")
        lines += [f"- {x['text']} ({x['link']})" for x in card["promises"]["mine"]]
    if card["promises"]["theirs"]:
        lines.append("\n### They asked for")
        lines += [f"- {x['value']}" for x in card["promises"]["theirs"]]
    if card["threads"]:
        lines.append("\n### Latest mail")
        lines += [f"- {_day(t['at'])} {t['subject'] or '(no subject)'} ({t['link']})"
                  for t in card["threads"]]
    return "\n".join(lines)


@span("brainBrief.person", kind="getter")  # noqa: F821 - guest global
def person(person_id, limit=20):
    """The card for one person → {person, facts, proposed, promises, threads, markdown}.

    `person` adds `addressForm` and `language` from their facts; `facts` are
    current facts in six groups, each with `quote` and `source`; `promises`
    is {mine: open promises to them, theirs: what they asked you for};
    `threads` are the latest mails with them. A person the user keeps out of
    the brain raises person_excluded."""
    view = recall.person(person_id, limit=limit)
    facts = view["facts"]
    prefs = facts.get("preferences") or []
    card = {
        "person": {**view["person"],
                   "addressForm": _first(prefs, "preference.address_form"),
                   "language": _first(prefs, "preference.language")},
        "facts": facts,
        "proposed": view["proposed"],
        "promises": {
            "mine": [p for p in view["promises"] if p.get("status") == "open"],
            "theirs": [f for f in facts.get("history") or []
                       if f.get("kind") == "history.asked_for"]},
        "threads": view["mail"][:THREADS],
    }
    card["markdown"] = render(card)
    return card
