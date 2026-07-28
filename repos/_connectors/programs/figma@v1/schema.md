### me() [getter]
Current Figma account `{ok, id, handle, email, imgUrl}`; doubles as
the token validator.

### get_file_meta(file_key) [getter]
Lightweight metadata `{ok, fileKey, name, lastModified, editorType,
thumbnailUrl}` — use for "exists / changed?" before pulling text.
`file_key` accepts a raw key or a pasted figma.com URL.

### list_comments(file_key) [getter]
All comments on a file, markdown-rendered, threaded via `parentId`:
`{ok, fileKey, count, comments: [{id, author, message, createdAt,
resolvedAt, parentId}]}`.

### get_file_text(file_key, depth?) [getter]
Every TEXT layer's content from the depth-limited node tree (`depth`
default 8): `{ok, fileKey, name, lastModified, depth, textNodes:
[{nodeId, page, name, text}]}`. Never returns the raw tree.

### list_project_files(project_id) [getter]
Files in a project: `{ok, projectId, name, count, files}`. Needs the
OAuth-only projects:read scope — a plain PAT usually gets 403.

### list_team_projects(team_id) [getter]
Projects in a team (id from a figma.com/team/:id/... URL): `{ok,
teamId, name, count, projects}`. Same projects:read caveat.
