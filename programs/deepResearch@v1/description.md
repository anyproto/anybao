Deep research: takes ONE question, researches it in four phases —
grounded initial answer, LLM-generated follow-up questions (3-7),
grounded answers for each, then pages written into the space: one
sub-page per follow-up and an overview page linking them
(`any://space/object` urls) with the deduped source list. The overview
page is the hub — no separate collection objects.

```python
dr = use("deepResearch@v1")
result = dr.research(space, "state of on-device LLM inference 2026",
                     {"chatId": chat_id})   # optional progress bubbles
```

Slow (30s-3min) and writes multiple objects — reach for it when the
user asks for research/a report/a deep dive, not for a quick fact
(that's `webSearch@v1`). Tell the user the overview page name when
done. Returns `{ok, overviewPageId, subPages, answer, sources, ...}`;
provider failures come back as `{ok: False, error}` — never raises.
