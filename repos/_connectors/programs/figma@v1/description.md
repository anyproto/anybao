Figma connector, read-only: account identity, lightweight file
metadata (cheap change checks), threaded file comments (markdown), and
the extracted TEXT-layer content of a design file — deliberately never
the multi-megabyte raw node tree. Figma has no "list all my files"
API: pass a file key or a pasted figma.com URL (file/design/board/
proto links all parse). Project/team listing methods exist but need
the OAuth-only projects:read scope — a plain PAT 403s there, and the
error says so. All methods return `{ok, ...}` or `{ok: false, error}`
with actionable messages. Auth is a host-injected Personal Access
Token (`connector.key.figma`, X-Figma-Token header); the token never
enters guest code or the trace. Rate limits are tight and per-minute —
429 backoff built in.
