from anybao.deploy import Deployer, ProgramSource, load_programs

PROG = "def main(args):\n    return 1\n"
TOOL_MD = "## Tool Description\n\nDoes a thing.\n\n## Tool Schema\n### go(x) [getter]\n\nruns go.\n"


def test_load_programs_parses_name_version(tmp_path):
    (tmp_path / "websearch@v1.py").write_text(PROG)
    (tmp_path / "websearch@v1.md").write_text(TOOL_MD)
    (tmp_path / "notaprogram.py").write_text("x")  # no @vN → skipped
    progs = load_programs(tmp_path)
    assert len(progs) == 1
    assert progs[0].name == "websearch" and progs[0].version == "v1"
    assert progs[0].tool_md == TOOL_MD


def test_fingerprint_over_split_form_stable():
    p = ProgramSource("t", "v1", PROG, TOOL_MD)
    assert p.fingerprint() == ProgramSource("t", "v1", PROG, TOOL_MD).fingerprint()
    # code change → different fingerprint
    assert p.fingerprint() != ProgramSource("t", "v1", PROG + "# x", TOOL_MD).fingerprint()
    # tool-doc change → different fingerprint
    assert p.fingerprint() != ProgramSource("t", "v1", PROG, TOOL_MD + "more").fingerprint()


class FakeClient:
    """Records writes; simulates an empty then populated space."""

    def __init__(self):
        self.objects = {}          # id -> props
        self.datasets = {}         # (oid, dataset) -> {recid: value}
        self.writes = []
        self._next = 0

    def query_objects(self, space, filter=None, limit=None, **kw):
        # match by program.name/version
        want_name = (filter or {}).get("program.name")
        want_ver = (filter or {}).get("program.version")
        for oid, props in self.objects.items():
            p = props.get("program", {})
            if p.get("name") == want_name and p.get("version") == want_ver:
                return [{"id": oid}]
        return []

    def create_object(self, space, body):
        self._next += 1
        oid = f"prog{self._next}"
        ip = body.get("initialProperties", {})
        self.objects[oid] = {"program": ip.get("program", {})}
        return {"objectId": oid}

    def set_properties(self, space, oid, type_id, patch):
        self.objects[oid].setdefault(type_id, {}).update(patch)

    def upsert_record(self, space, oid, dataset, rid, value):
        self.datasets.setdefault((oid, dataset), {})[rid] = value
        self.writes.append((dataset, rid))

    def query(self, space, oid, dataset, **kw):
        d = self.datasets.get((oid, dataset), {})
        return [{"id": rid, **v} for rid, v in d.items()]

    def modify(self, space, body):
        for r in body["records"]:
            self.datasets.get((body["objectId"], body["dataset"]), {}).pop(r["id"], None)


def test_deploy_creates_then_unchanged():
    fc = FakeClient()
    d = Deployer(fc, space="agent")
    p = ProgramSource("websearch", "v1", PROG, TOOL_MD)

    assert d.deploy_one(p) == "created"
    # source + description + one method written
    prog_props = fc.objects["prog1"]["program"]
    assert (prog_props["name"], prog_props["version"]) == ("websearch", "v1")
    assert prog_props["any_tool"] is True
    assert fc.datasets[("prog1", "program_source")]["main"]["code"] == PROG
    assert "go" in fc.datasets[("prog1", "program_methods")]

    # redeploy identical → hash-gated skip
    assert d.deploy_one(p) == "unchanged"


def test_deploy_updates_on_code_change():
    fc = FakeClient()
    d = Deployer(fc, space="agent")
    d.deploy_one(ProgramSource("t", "v1", PROG, TOOL_MD))
    status = d.deploy_one(ProgramSource("t", "v1", PROG + "# changed", TOOL_MD))
    assert status == "updated"
    assert "# changed" in fc.datasets[("prog1", "program_source")]["main"]["code"]


def test_deploy_without_tooldoc_is_not_a_tool():
    fc = FakeClient()
    d = Deployer(fc, space="agent")
    d.deploy_one(ProgramSource("lib", "v1", PROG, tool_md=""))
    assert fc.objects["prog1"]["program"]["any_tool"] is False
    assert ("prog1", "program_description") not in fc.datasets
