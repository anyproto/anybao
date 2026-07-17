### delegate(space, task, opts?) [program]
Run `task` (a self-contained instruction string) in a quiet child
toolcaller loop over `space`. The child composes its system prompt
from the same space (all tools available) but starts with fresh
context — no chat history, no auto-recall, and it cannot post to chat;
ceilings bound the run (default maxTurns 30). `opts`:
`{"maxTurns"?, "maxTokensTotal"?, "tier"?, "agentName"? (default
"bao-sub"), "chatId"?}`. Returns `{report, stop, turns, tokens}` —
`report` is the child's final reply text; `stop` is `done` or `wrapup`
(a ceiling hit — the report is then a progress summary, not a
completion). Blocks until the child finishes; sequential only.
