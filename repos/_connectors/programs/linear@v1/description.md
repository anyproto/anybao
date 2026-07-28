Linear (issue tracker) connector: read your assigned issues, workspace
issues (optionally incremental by updatedAt), teams, one issue with
full description, and issue comments; write via update_issue and
create_comment. All methods return `{ok, ...}` or `{ok: false, error}`
with actionable messages — a missing/rejected API key comes back as an
error explaining how to connect, never a traceback. Auth is a
host-injected credential (`connector.key.linear`); the key never
enters guest code or the trace. Note Linear mutations need the issue
UUID, not "ENG-123" — fetch the issue first.
