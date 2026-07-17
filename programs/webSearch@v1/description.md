Grounded web search: each query goes to Gemini with Google Search
grounding and comes back as ONE formatted string — a concrete 4-8
sentence synthesized answer with the grounding sources (real
destination urls) listed inline. Pass several queries at once (one
round-trip) when researching from multiple angles:

```python
ws = use("webSearch@v1")
results = ws.search("rust wasmtime fuel metering",
                    "wasmtime epoch interruption vs fuel")
```

A failed query yields an `[ERROR] …` string in its slot; the other
queries still return. Use this for anything that needs current facts —
prices, versions, dates, news, docs. For a multi-page investigation
with pages written into the space, reach for `deepResearch@v1` instead.
