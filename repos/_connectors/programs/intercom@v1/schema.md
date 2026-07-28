### me() [getter]
The authenticated app/admin context — cheapest connectivity check.
Returns `{ok, me}`.

### list_conversations(open?, sort?, order?, per_page?, starting_after?) [getter]
Conversation SUMMARIES (no parts), newest updated first. `open`
filters open/closed; `per_page` max 150. Returns `{ok, conversations,
pages}` — next page via `pages.next.starting_after`.

### search_conversations(query, per_page?, starting_after?) [getter]
Filtered search via the Intercom query DSL — `{field, operator,
value}` leaves, AND/OR groups. Returns `{ok, conversations, pages}`.

### get_conversation(id, plaintext?) [getter]
One conversation WITH its message parts (the transcript; capped by
Intercom at the 500 most recent parts). `plaintext` defaults true
(plain-text bodies). Returns `{ok, conversation}`.

### list_contacts(per_page?, starting_after?) [getter]
Contacts and leads. Returns `{ok, data, pages}`.

### search_contacts(query, per_page?, starting_after?) [getter]
Filtered contact search (by email, custom attribute, ...) via the
query DSL. Returns `{ok, data, pages}`.

### list_articles(per_page?, starting_after?) [getter]
Help-center articles. Returns `{ok, data, pages}`.
