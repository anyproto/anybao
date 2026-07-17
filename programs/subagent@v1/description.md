Delegate a self-contained subtask to a fresh agent loop and get its
final report back as a value — nothing is posted to chat and no history
is loaded or written; the child sees only the task text (plus the same
tools you have).

```python
sub = use("subagent@v1")
r = sub.delegate(space, "Survey the open tasks in this space and "
                        "summarize what is overdue, with object ids.")
r["report"]   # the child's final reply
```

Delegate when a subtask is self-contained and its intermediate steps
would only clutter your context (a survey, a batch transformation, a
research errand). Write the task like a good ticket: goal, inputs (ids,
names), and what the report must contain. The child starts blank — it
knows nothing of this conversation. Sequential (the call blocks until
the child finishes); don't nest delegations beyond one level.
