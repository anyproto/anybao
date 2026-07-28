Intercom connector, read-only: customer conversations (summaries via
list, full transcripts via get, filtered search via Intercom's query
DSL), contacts/leads (list + search), and help-center articles.
Ingestion only — no writes. Cursor pagination throughout: responses
carry `pages`, pass `pages.next.starting_after` back as
`starting_after`. All methods return `{ok, ...}` or `{ok: false,
error}` with actionable messages. Auth is a host-injected Access
Token (`connector.key.intercom`, Bearer + the required
Intercom-Version 2.15 pin); the token never enters guest code or the
trace. US-hosted API base; EU/AU workspaces are not yet supported.
