Attio CRM connector, read-only: discover the workspace's objects and
their attribute schemas, query records (people / companies / deals)
with Attio's filter/sort DSL, page list (pipeline) entries, read
free-text notes, and resolve workspace members. Ingestion only —
never writes back to Attio. All methods return `{ok, ...}` or
`{ok: false, error}` with actionable messages. Auth is a host-injected
access token (`connector.key.attio`); the token never enters guest
code or the trace. Gotcha handled in errors: fresh Attio tokens have
NO scopes and 403 until read scopes are granted — the errors list
exactly which.
