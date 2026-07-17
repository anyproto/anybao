Conversation history composition over `agent_turns` / `agent_chunks`
(turns and chunks live on the chat object in the user space). Thin
reads plus the token-budgeted boot window — newest raw turns at full
resolution, older history as one-line chunk summaries ascending by
level, so ALL history is present at decreasing resolution:

```python
h = use("history@v1")
turns = list(reversed(h.recent_turns(c, space, chat_id, 200)))
```

`build_turn` shapes the turn payload the loop persists; expand any
chunk by querying `agent_chunks` for its `#seq` and reading the raw
turns in its `fromSeq`–`toSeq` range.
