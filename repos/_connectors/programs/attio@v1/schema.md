### whoami() [getter]
Credential/scope check — lists workspace members (no token-self
endpoint exists). Returns `{ok, connected, memberCount, members}`.

### list_objects() [getter]
The workspace's standard/custom objects and their slugs (people /
companies / deals / ...). Returns `{ok, objects}`.

### list_attributes(object) [getter]
One object's attribute schema — which slugs exist to read/map.
`object` is a slug or UUID. Returns `{ok, attributes}`.

### query_records(object, filter?, sorts?, limit?, offset?) [getter]
The workhorse: page records of one object with Attio's filter/sort
DSL. `limit` default 100, max 500; `offset` default 0. Returns `{ok,
records, count, offset, limit}`.

### get_record(object, record_id) [getter]
Single record by UUID. Returns `{ok, record}`.

### list_lists() [getter]
Pipelines/segments. Returns `{ok, lists}`.

### query_list_entries(list, filter?, sorts?, limit?, offset?) [getter]
Page one list's entries — same semantics as query_records. Returns
`{ok, entries, count, offset, limit}`.

### list_notes(limit?, offset?) [getter]
Free-text notes attached to records. `limit` default 50, max 500.
Returns `{ok, notes, count, offset, limit}`.

### list_workspace_members() [getter]
Team roster for owner/assignee resolution. Returns `{ok, members}`.
