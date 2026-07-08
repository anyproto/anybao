"""Overlays v1 — spaces as package repositories (plan § Overlays;
ADR-004 aliases).

An overlay is a REGULAR space holding programs in the standard format —
nothing new on the wire. v1 ships the three contract pieces:

- **Aliases via the config cascade**: `overlays.<alias>` keys resolve
  to space ids; `agent:` is just the conventional alias for the
  agent-code overlay, `private:` stays resolver-built-in. Unqualified
  names NEVER resolve into an overlay (ADR-004 discipline — an overlay
  is only reachable through its alias; local always wins).
- **Manifest object per overlay**: name, description, anyrt compat,
  and the program catalog (derived from what is actually deployed).
- **Frozen versions**: a published `name@vN` never mutates — the
  Deployer refuses in-place edits in frozen mode; an edit bumps vN.
"""

from __future__ import annotations

from .anyclient import AnyClient, AnyError
from .config import Config
from .deploy import PROGRAM_TYPE
from .modules import AnyModuleResolver

OVERLAY_PREFIX = "overlays."
MANIFEST_TYPE = "overlay_manifest"


def aliases_from_config(config: Config) -> dict[str, str]:
    """{alias: spaceId} from the cascade — device overrides win, per
    ADR-006 §3 resolution."""
    return {alias: sid for alias, sid in config.under(OVERLAY_PREFIX).items()
            if isinstance(sid, str) and sid}


def resolver_from_config(client: AnyClient, config: Config, *,
                         current_space: str,
                         private_space: str | None = None) -> AnyModuleResolver:
    return AnyModuleResolver(client, current_space=current_space,
                             private_space=private_space,
                             aliases=aliases_from_config(config))


def catalog_of(client: AnyClient, space: str) -> list[str]:
    """The overlay's deployed program specs, sorted."""
    recs = client.query_objects(space, filter={f"{PROGRAM_TYPE}.name":
                                               {"$exists": True}})
    specs = []
    for r in recs:
        group = r.get(PROGRAM_TYPE) or {}
        if group.get("name") and group.get("version"):
            specs.append(f"{group['name']}@{group['version']}")
    return sorted(specs)


def publish_manifest(client: AnyClient, space: str, *, name: str,
                     description: str = "", anyrt_compat: str = "0",
                     ts: int = 0) -> dict:
    """Find-or-create the overlay's singleton manifest object and
    refresh its record (catalog derived, never hand-listed)."""
    import contextlib
    with contextlib.suppress(AnyError):  # existing type is the fine case
        client.create_type(space, {"name": "Overlay Manifest",
                                   "xKey": MANIFEST_TYPE})
    rows = client.query_objects(space,
                                filter={f"{MANIFEST_TYPE}.name": {"$exists": True}},
                                limit=1)
    if rows:
        oid = rows[0]["id"]
    else:
        oid = client.create_object(space, {
            "types": [MANIFEST_TYPE],
            "initialProperties": {"any": {"name": f"overlay: {name}"},
                                  MANIFEST_TYPE: {"name": name}}})["objectId"]
    manifest = {"name": name, "description": description,
                "anyrtCompat": anyrt_compat, "catalog": catalog_of(client, space),
                "publishedAt": ts}
    client.upsert_record(space, oid, MANIFEST_TYPE, "main", manifest)
    return {"objectId": oid, **manifest}


def read_manifest(client: AnyClient, space: str) -> dict | None:
    rows = client.query_objects(space,
                                filter={f"{MANIFEST_TYPE}.name": {"$exists": True}},
                                limit=1)
    if not rows:
        return None
    recs = client.query(space, rows[0]["id"], MANIFEST_TYPE,
                        filter={"id": "main"}, limit=1)
    return recs[0] if recs else None
