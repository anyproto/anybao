"""Golden-conversation builder: scripted llm + http effects (all
deterministic), REAL cells in the wasi guest. Recording this scenario
yields the committed fixture; replaying the fixture re-runs the same
cells with every effect answered from the trace."""

from pathlib import Path

from anyrt import trace as tr
from anyrt.effects import Broker, Registry, effect
from anyrt.wasi import WasiEngine

KERNEL = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"
FIXTURE = Path(__file__).parent / "fixtures" / "golden-conv.jsonl"

# Seeded from a real bao conversation shape: fetch two pages, compare
# sizes, reply. Cells are Python (fresh-written; ADR clean-cut rule).
CELL_1 = (
    "a = effect('http.get', {'url': 'https://example.test/hn-digest'})\n"
    "b = effect('http.get', {'url': 'https://example.test/tc-digest'})\n"
    "sizes = {'hn': len(a['body']), 'tc': len(b['body'])}\n"
    "print('sizes', sizes)\n"
    "max(sizes, key=sizes.get)"
)

SCRIPTED_REPLIES = [
    {
        "parts": [
            {"type": "text", "text": "Comparing the two digests."},
            {"type": "tool_call", "id": "cell_001", "name": "run_cell",
             "args": {"code": CELL_1}},
        ],
        "stop": "tool",
        "usage": {"in": 100, "out": 50},
    },
    {
        "parts": [{"type": "text", "text": "The HN digest is the bigger one."}],
        "stop": "done",
        "usage": {"in": 160, "out": 20},
    },
]


def build_registry(replies: list[dict]) -> Registry:
    reg = Registry()
    queue = list(replies)

    @effect("llm.chat", kind="read", registry=reg)
    def llm_chat(ctx, messages, system="", tier="codegen"):
        return queue.pop(0)

    @effect("http.get", kind="read", registry=reg)
    def http_get(ctx, url):
        body = "hn " * 40 if "hn" in url else "tc " * 12
        return {"status": 200, "body": body}

    return reg


def run_golden(mode: str, records: list[dict] | None = None):
    from anybao.loop import run_conversation

    reg = build_registry(SCRIPTED_REPLIES)
    writer = tr.TraceWriter(run={"id": "golden-conv", "program": "toolcaller@m0"})
    if mode == "record":
        broker = Broker(reg, writer)
    elif mode == "replay":
        assert records is not None
        broker = Broker(reg, writer, mode="replay", cursor=tr.ReplayCursor(records))
    else:
        raise ValueError(mode)
    executor = WasiEngine(broker, kernel_wasm=KERNEL)
    replies = run_conversation("which digest is bigger, hn or tc?",
                               broker=broker, executor=executor)
    return replies, writer, broker
