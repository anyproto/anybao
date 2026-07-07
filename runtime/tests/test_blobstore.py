from anyrt import trace as tr
from anyrt.blobstore import FileSidecarStore


def test_sidecar_roundtrip(tmp_path):
    w = tr.TraceWriter(run={"id": "b"}, blob_threshold=32)
    k = tr.input_key("e", {})
    w.effect(effect="e", cell=None, input={}, key=k, output={"body": "z" * 500})
    base = tmp_path / "t.jsonl"
    store = FileSidecarStore()
    store.put(w.blobs, base)
    loaded = store.load(base)
    assert loaded == w.blobs
    ref = w.records[1]["output"]
    assert tr.resolve_blobs(ref, loaded) == {"body": "z" * 500}


def test_sidecar_absent_returns_empty(tmp_path):
    assert FileSidecarStore().load(tmp_path / "none.jsonl") == {}
