"""Overlays v1 — config aliases, manifest publish, frozen versions."""

import pytest
from anybao.anyclient import AnyClient
from anybao.config import Config, DictConfigStore
from anybao.deploy import Deployer, FrozenVersionError, ProgramSource
from anybao.overlays import (
    aliases_from_config,
    catalog_of,
    publish_manifest,
    read_manifest,
    resolver_from_config,
)

PROG = "def main(args):\n    return 1\n"


def config_with(records=None, defaults=None):
    cfg = Config(DictConfigStore(records or {}))
    for k, v in (defaults or {}).items():
        cfg.define(k, default=v)
    return cfg


def test_aliases_resolve_through_the_cascade():
    cfg = config_with(
        records={"overlays.std": {"value": "space-std",
                                  "localValue": "space-std-dev"},
                 "overlays.agent": {"value": "space-agent"},
                 "overlays.broken": {"value": ""}},
        defaults={"overlays.extra": "space-extra"})
    assert aliases_from_config(cfg) == {
        "std": "space-std-dev",       # device (localValue) wins
        "agent": "space-agent",
        "extra": "space-extra",       # defined default participates
    }                                  # empty value dropped


def test_config_under_strips_prefix():
    cfg = config_with(records={"overlays.std": {"value": "s"}},
                      defaults={"loop.max_turns": 9})
    assert cfg.under("overlays.") == {"std": "s"}


class FakeOverlaySpace:
    """Programs + manifest object storage for one space."""

    def __init__(self):
        self.objects = []            # [{id, program: {...}}]
        self.records = {}            # (oid, dataset, rid) -> value
        self.next = 0

    def __call__(self, method, path, body):
        if path.endswith("/types"):
            return 409, {"error": {"code": "type.exists", "message": "dup"}}
        if path.endswith("/objects/query"):
            flt = body.get("filter", {})
            group = next(iter(flt)).split(".")[0]
            rows = [o for o in self.objects if group in o]
            return 200, {"records": rows[:body.get("limit") or len(rows)]}
        if path.endswith("/objects"):
            self.next += 1
            oid = f"o{self.next}"
            props = body.get("initialProperties", {})
            self.objects.append({"id": oid, **{k: v for k, v in props.items()
                                               if k != "any"}})
            return 200, {"objectId": oid}
        if path.endswith("/modify"):
            rec = body["records"][0]
            self.records[(body["objectId"], body["dataset"], rec["id"])] = \
                rec["ops"][0]["value"]
            return 200, {"versionId": "v", "changeId": "c", "recordIds": [rec["id"]]}
        if path.endswith("/query"):
            vals = [v for (oid, ds, rid), v in self.records.items()
                    if oid == body["objectId"] and ds == body["dataset"]]
            return 200, {"records": [{"id": "main", **v} for v in vals]}
        return 404, {"error": {"code": "unknown", "message": path}}


def test_publish_manifest_derives_catalog_and_round_trips():
    fake = FakeOverlaySpace()
    fake.objects = [{"id": "p1", "program": {"name": "rollup", "version": "v1"}},
                    {"id": "p2", "program": {"name": "linkgen", "version": "v1"}},
                    {"id": "p3", "program": {"name": "half-made"}}]  # no version
    client = AnyClient(fake)
    out = publish_manifest(client, "s1", name="agent-code",
                           description="the agent overlay", ts=123)
    assert out["catalog"] == ["linkgen@v1", "rollup@v1"]
    got = read_manifest(client, "s1")
    assert got["name"] == "agent-code" and got["publishedAt"] == 123
    # republish reuses the singleton object
    publish_manifest(client, "s1", name="agent-code", ts=124)
    manifests = [o for o in fake.objects if "overlay_manifest" in o]
    assert len(manifests) == 1


def test_catalog_of_skips_incomplete_programs():
    fake = FakeOverlaySpace()
    fake.objects = [{"id": "p1", "program": {"name": "x", "version": "v2"}}]
    assert catalog_of(AnyClient(fake), "s1") == ["x@v2"]


# --- frozen versions ------------------------------------------------------------

class FakeProgramSpace:
    def __init__(self, existing_code=None):
        self.existing = existing_code

    def __call__(self, method, path, body):
        if path.endswith("/objects/query"):
            recs = [{"id": "p1"}] if self.existing is not None else []
            return 200, {"records": recs}
        if path.endswith("/query"):
            ds = body["dataset"]
            if ds == "program_source" and self.existing is not None:
                return 200, {"records": [{"id": "main", "code": self.existing}]}
            return 200, {"records": []}
        if path.endswith("/modify") or path.endswith("/objects"):
            return 200, {"versionId": "v", "changeId": "c", "recordIds": ["r"],
                         "objectId": "p1"}
        return 200, {}


def test_frozen_deployer_rejects_in_place_edit_allows_unchanged_and_new():
    changed = Deployer(AnyClient(FakeProgramSpace(existing_code=PROG)),
                       space="s1", frozen=True)
    with pytest.raises(FrozenVersionError, match="bump the version"):
        changed.deploy_one(ProgramSource("t", "v1", PROG + "# edit"))

    unchanged = Deployer(AnyClient(FakeProgramSpace(existing_code=PROG)),
                         space="s1", frozen=True)
    assert unchanged.deploy_one(ProgramSource("t", "v1", PROG)) == "unchanged"

    new = Deployer(AnyClient(FakeProgramSpace(existing_code=None)),
                   space="s1", frozen=True)
    assert new.deploy_one(ProgramSource("t", "v2", PROG)) == "created"


def test_resolver_from_config_wires_aliases():
    cfg = config_with(records={"overlays.std": {"value": "space-std"}})
    seen = []

    def send(method, path, body):
        seen.append(path)
        if path.endswith("/objects/query"):
            return 200, {"records": [{"id": "p1"}]}
        if path.endswith("/query"):
            return 200, {"records": [{"id": "main", "code": PROG, "_addSeq": 1}]}
        return 404, {"error": {"code": "unknown", "message": path}}

    r = resolver_from_config(AnyClient(send), cfg, current_space="user-space")
    resolved = r("std:tool@v1")
    assert resolved.space_id == "space-std" and resolved.source == PROG
    assert any("space-std" in p for p in seen)
