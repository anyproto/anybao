"""programs/ui@v1 through the REAL guest kernel (ADR-009 §6 /run).

Pinned: main()'s method dispatch (unknown method / bad args return
{ok: False}, never a traceback), the classify-tier call shape, summary
written back as the ONLY field, cached short-circuit (+ force), and
the failure contract — missing record / model error / empty reply all
return {ok: False} without touching the record."""

import kernelenv


class FakeAny:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.upserts = []

    def query(self, space, object_id, dataset, filter=None, limit=None):
        want = (filter or {}).get("id", {}).get("$in", [])
        return [r for r in self.rows if r["id"] in want]

    def upsert_records(self, space, object_id, dataset, records,
                       page_size=None):
        self.upserts.append((space, object_id, dataset, records))
        return {"created": 0, "updated": len(records), "skipped": 0}


REC = {"id": "aa11", "subject": "Quarterly kumquat report",
       "from": "kim@example.com", "date": "Thu, 21 Aug 2026",
       "body": "the kumquat yield doubled this quarter", "summary": ""}

ARGS = {"method": "emailSummary", "space": "sp", "mailbox_id": "mb1",
        "record_id": "aa11"}


def llm_ok(messages, system, tier, tools, max_tokens):
    assert tier == "classify" and max_tokens == 256
    assert "kumquat yield" in messages[0]["parts"][0]["text"]
    return {"parts": [{"type": "text", "text": " Yield doubled. "}],
            "stop": "done", "usage": {"in": 50, "out": 8}}


def load(fake, llm):
    app = kernelenv.load_kernel(effect=lambda n, p: {}, any_client=fake,
                                llm_chat=llm)
    return app.use("ui@v1")


def test_email_summary_writes_only_summary_field():
    fake = FakeAny(rows=[dict(REC)])
    out = load(fake, llm_ok).main(dict(ARGS))
    assert out == {"ok": True, "summary": "Yield doubled."}
    assert fake.upserts == [("sp", "mb1", "email_messages",
                             [{"id": "aa11",
                               "fields": {"summary": "Yield doubled."}}])]


def test_existing_summary_short_circuits_unless_forced():
    fake = FakeAny(rows=[dict(REC, summary="already there")])
    mod = load(fake, llm_ok)
    assert mod.main(dict(ARGS)) == {
        "ok": True, "summary": "already there", "cached": True}
    assert fake.upserts == []
    forced = mod.main(dict(ARGS, force=True))
    assert forced == {"ok": True, "summary": "Yield doubled."}
    assert len(fake.upserts) == 1


def test_model_failure_writes_nothing():
    def llm_boom(**kw):
        raise RuntimeError("model down")
    fake = FakeAny(rows=[dict(REC)])
    out = load(fake, llm_boom).main(dict(ARGS))
    assert out["ok"] is False and "failed" in out["error"]
    assert fake.upserts == []


def test_empty_model_reply_writes_nothing():
    def llm_empty(**kw):
        return {"parts": [], "stop": "done", "usage": {"in": 1, "out": 0}}
    fake = FakeAny(rows=[dict(REC)])
    out = load(fake, llm_empty).main(dict(ARGS))
    assert out == {"ok": False, "error": "model returned no text"}
    assert fake.upserts == []


def test_missing_record_and_args_error():
    fake = FakeAny()
    mod = load(fake, llm_ok)
    assert "not found" in mod.main(dict(ARGS, record_id="ghost"))["error"]
    assert "required" in mod.main(dict(ARGS, space=""))["error"]
    assert fake.upserts == []


def test_dispatch_contract_never_tracebacks():
    mod = load(FakeAny(), llm_ok)
    unknown = mod.main({"method": "nope"})
    assert unknown["ok"] is False and "unknown method" in unknown["error"]
    assert "emailSummary" in unknown["error"]
    bad = mod.main({"method": "emailSummary", "space": "sp", "bogus": 1})
    assert bad["ok"] is False and "bad args" in bad["error"]
    assert "not found" not in bad["error"]
