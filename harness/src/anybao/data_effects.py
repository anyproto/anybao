"""Primitive `any.*` data-access effects — the guest's path to space data
(ADR-002 §3-4; the effect catalog is "specified as built, doc-per-effect").

These are the low-level primitives, NOT the agent's ergonomic surface: the
anyhelper / recall / memory *tools* are space-resident programs (ADR-004 §1
"pre-bound facades from the boot prelude") composed over these. A primitive
mirrors one anyclient call 1:1, adds nothing ergonomic (no catalog
resolution, no default-space — the tool program owns that, helper-style §4-5),
and is `read`/`mutate`-classified so replay, mocking, and capability checks
work exactly as they do for `http`/`chat`/`llm`. `space` is always an explicit
arg (the agent has arbitrary-spaceId scope — cross-space is normal).

Doc-per-effect: docs/effects/data.md.
"""

from __future__ import annotations

from anyrt.effects import Registry, effect

from .anyclient import AnyClient

READ = "data.read"
WRITE = "data.write"


def register_data_effects(registry: Registry, client: AnyClient) -> None:
    """Register the primitive `any.*` data effects against a bound client.
    Reads are re-executable (cap data.read); writes are not (data.write)."""

    # --- reads ---------------------------------------------------------------
    @effect("any.query", kind="read", registry=registry, cap=READ)
    def any_query(ctx, space, object_id, dataset, filter=None, sort=None,
                  limit=None, offset=None):
        """Snapshot of one object's dataset (chat_messages, agent_turns,
        editor_blocks, …). Returns the record list."""
        opts = _opts(filter=filter, sort=sort, limit=limit, offset=offset)
        return client.query(space, object_id, dataset, **opts)

    @effect("any.query_objects", kind="read", registry=registry, cap=READ)
    def any_query_objects(ctx, space, filter=None, sort=None, limit=None, offset=None):
        """Snapshot over the per-space objects collection."""
        opts = _opts(filter=filter, sort=sort, limit=limit, offset=offset)
        return client.query_objects(space, **opts)

    @effect("any.search", kind="read", registry=registry, cap=READ)
    def any_search(ctx, space, query, scopes=None, limit=None, mode=None):
        """Index search — `{hits, mode, vectorStatus}` (docs/13-index.md)."""
        return client.search(space, query, scopes=scopes, limit=limit, mode=mode)

    @effect("any.aggregate", kind="read", registry=registry, cap=READ)
    def any_aggregate(ctx, space, pipeline):
        """MongoDB-style aggregation over the objects collection."""
        return client.aggregate_objects(space, pipeline)

    @effect("any.get_markdown", kind="read", registry=registry, cap=READ)
    def any_get_markdown(ctx, space, object_id):
        """Render an editor object's blocks to markdown."""
        return client.get_markdown(space, object_id)

    @effect("any.list_properties", kind="read", registry=registry, cap=READ)
    def any_list_properties(ctx, space, type_id):
        """A type's property catalog — `[{id, name, xKey, kind}]`; the
        anyhelper tool caches this for xKey→propId resolution."""
        return client.list_properties(space, type_id)

    # --- writes --------------------------------------------------------------
    @effect("any.modify", kind="mutate", registry=registry, cap=WRITE)
    def any_modify(ctx, space, body):
        """The general record write (create/update/delete, scoped) — POST
        /modify. Returns the ModifyResult `{versionId, changeId, recordIds}`."""
        return client.modify(space, body)

    @effect("any.create_object", kind="mutate", registry=registry, cap=WRITE)
    def any_create_object(ctx, space, body):
        """Create an object — `{objectId}`."""
        return client.create_object(space, body)

    @effect("any.create_type", kind="mutate", registry=registry, cap=WRITE)
    def any_create_type(ctx, space, body):
        """Create a user type — `{typeId}`."""
        return client.create_type(space, body)

    @effect("any.add_property", kind="mutate", registry=registry, cap=WRITE)
    def any_add_property(ctx, space, type_id, body):
        """Add a property to a user type — `{propId}`."""
        return client.add_property(space, type_id, body)

    @effect("any.upsert_record", kind="mutate", registry=registry, cap=WRITE)
    def any_upsert_record(ctx, space, object_id, dataset, record_id, value):
        """Upsert one dataset record by id."""
        return client.upsert_record(space, object_id, dataset, record_id, value)


def _opts(**kw) -> dict:
    """Drop None so absent options don't reach the wire as nulls."""
    return {k: v for k, v in kw.items() if v is not None}
