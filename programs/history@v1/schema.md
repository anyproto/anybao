### recent_turns(client, space, chat_id, limit) [getter]
Newest turns first (descending seq) from the chat object's `agent_turns`.

### chunks_at_level(client, space, chat_id, level, limit) [getter]
Newest chunks of one level first (descending seq) from `agent_chunks`.

### render_boot_window(raw_turns, chunks_by_level, total_tokens?, raw_tail_fraction?) [getter]
Compose the boot context as neutral messages, oldest→newest: raw tail at full resolution, remaining budget filled with chunk lines (each carrying its drill-down `#seq` + child range); chunks fully covered by the tail are skipped. `raw_turns` ascending by seq, `chunks_by_level` = level → chunks ascending by seq.

### raw_tail(raw_turns, total_tokens?, raw_tail_fraction?) [getter]
Just the full-resolution slice: newest raw turns filling the raw-tail budget, returned oldest→newest. Its min seq is the auto-recall deep-history boundary.

### build_turn(user_text, outcome, think?, effects?, message_ids?, trace_ref?, user_name?, from_agent?, llm?) [getter]
Shape the agent_turns v2 payload (seq is server-assigned; write it with `c.append_turn`). `outcome` is the loop outcome dict; `replies` = what the user saw, `think` = narration that did NOT go to chat.

### approx_tokens(text) [getter]
Cheap proxy tokenizer — ~4 chars/token.
