Granola (AI meeting notes) connector, read-only: list meeting notes
(newest-first, optionally filtered by created_after / folder, cursor-
paginated), fetch one note with its AI summary and optionally the raw
transcript, and list the folder tree. No writes — the public API has
none. All methods return `{ok, ...}` or `{ok: false, error}` with
actionable messages. Auth is a host-injected grn_ API key
(`connector.key.granola`) — Business/Enterprise plans only; the key
never enters guest code or the trace. Note: a meeting appears only
after its summary + transcript finish generating.
