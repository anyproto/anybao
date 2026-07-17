Model calls in the neutral message shape — the same surface the
conversation loop itself runs on. One method:

```python
reply = use("llm@v1").chat(messages, system="…", tier="classify")
```

The tier's provider config comes from the runtime; the API key is
injected host-side and never enters the guest. Use it for one-off
structured judgments (classification, extraction, scoring) inside a
cell — a sub-call, not a way to talk to the user. Message parts:
`{type: text|tool_call|tool_result|thinking, …}`; the reply is
`{parts, stop: done|tool|length, usage: {in, out}}`.
