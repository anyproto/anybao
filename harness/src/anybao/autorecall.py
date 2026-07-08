"""Auto-recall injection — ADR-007 §5: the recall-side STRUCTURAL
mechanism (memory behavior must not depend on model initiative).

At invocation start the harness runs `recall.search(user_message)` —
index-backed, no LLM call — and injects the top hits as a synthetic
recall tool call + tool result: evidence the model weighs and can
discount as stale, never prompt truth. Memory hits render as distilled
facts (provenance date + confidence); history hits as *related past
discussion* pointers (chunk drill-down handles, bodies on demand).
Guards: deep-history only (hits inside the boot window's raw tail are
skipped — auto-recall is the TOPICAL channel, the window owns RECENCY),
relevance threshold (generic messages inject nothing), token budget.
accessCount bumps on every injected memory item (§4.3, ON from day one).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime

from .anyclient import AnyClient
from .digest import approx_tokens
from .memory import Memory
from .recall import Recall

INJECT_SCOPES = ("agent", "history")
_HISTORY_DATASETS = ("agent_turns", "agent_chunks")


@dataclass
class AutoRecallPolicy:
    # Relevance gate — below it, inject nothing. The server's hybrid
    # search scores are RRF (k=60): rank-1 in ONE leg ≈ 0.0164, rank-1
    # in both ≈ 0.033 (live-calibrated 2026-07-08). 0.015 admits
    # near-top hits from either leg and drops deep-rank noise; a 0-1
    # similarity scale would need a very different constant (ADR-007
    # §7: scale normalization is an upstream question).
    min_score: float = 0.015
    max_memory: int = 5         # §5 "top 3–5 hits"
    max_history: int = 3        # §5 "capped at 2–3"
    token_budget: int = 1500
    tokenizer = staticmethod(approx_tokens)


def _date(ts) -> str:
    if not isinstance(ts, (int, float)) or not ts:
        return "undated"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def memory_line(item: dict) -> str:
    prov = _date(item.get("validFrom") or item.get("createdAt"))
    conf = item.get("confidence")
    suffix = f"(saved {prov}" + (f", confidence {conf}" if conf is not None else "") + ")"
    return f"- [{item.get('category', '?')}] {item.get('context', '')} {suffix}"


def history_line(rec: dict, dataset: str) -> str:
    if dataset == "agent_chunks":
        return (f"- related past discussion, {_date(rec.get('periodStart'))}: "
                f"{rec.get('summary', '')} [chunk #{rec.get('seq')} "
                f"(L{rec.get('level', 1)}), turns {rec.get('fromSeq')}–{rec.get('toSeq')}]")
    first = (rec.get("userText") or "").splitlines() or [""]
    return (f"- related past discussion, {_date(rec.get('createdAt'))}: "
            f"“{first[0][:100]}” [turn #{rec.get('seq')}]")


def covered_by_boot(rec: dict, dataset: str, boot_min_seq: int | None) -> bool:
    """Deep-history guard: a turn in the boot window's raw tail, or a
    chunk fully inside it, is already visible at full resolution."""
    if boot_min_seq is None:
        return False
    if dataset == "agent_turns":
        return rec.get("seq", -1) >= boot_min_seq
    if dataset == "agent_chunks":
        return rec.get("fromSeq", -1) >= boot_min_seq
    return False


def frame_messages(query: str, memory_lines: list[str], history_lines: list[str]) -> list[dict]:
    """Tool-result framing (§5): a synthetic recall call + its result,
    exactly the shape an explicit recall would produce."""
    sections = []
    if memory_lines:
        sections.append("Memories:\n" + "\n".join(memory_lines))
    if history_lines:
        sections.append("Related history:\n" + "\n".join(history_lines))
    if not sections:
        return []
    call_id = "autorecall_0"
    return [
        {"role": "assistant",
         "parts": [{"type": "tool_call", "id": call_id, "name": "recall",
                    "args": {"query": query, "scopes": list(INJECT_SCOPES)}}]},
        {"role": "user",
         "parts": [{"type": "tool_result", "call_id": call_id,
                    "content": "\n\n".join(sections), "is_error": False}]},
    ]


class AutoRecall:
    def __init__(self, client: AnyClient, space: str, *,
                 policy: AutoRecallPolicy | None = None,
                 recall: Recall | None = None, memory: Memory | None = None):
        self._c = client
        self._space = space
        self.policy = policy or AutoRecallPolicy()
        self._recall = recall or Recall(client, space)
        self._memory = memory or Memory(client, space)
        # (hit, item) pairs actually injected last call — the ROI log's input
        self.last_injected: list[tuple[dict, dict]] = []

    def messages_for(self, user_text: str, *, boot_min_seq: int | None = None) -> list[dict]:
        try:
            return self._inject(user_text, boot_min_seq)
        except Exception:
            return []  # fail-open: recall must never break the turn

    def _inject(self, user_text: str, boot_min_seq: int | None) -> list[dict]:
        p = self.policy
        hits = self._recall.search(user_text, scopes=INJECT_SCOPES,
                                   limit=(p.max_memory + p.max_history) * 2)
        relevant = [h for h in hits if h.get("score", 0) >= p.min_score]
        mem_hits = [h for h in relevant if h.get("dataset") == "agent_memory_items"]
        hist_hits = [h for h in relevant if h.get("dataset") in _HISTORY_DATASETS]
        if not mem_hits and not hist_hits:
            return []

        pairs = self._recall.hydrate(mem_hits + hist_hits)
        budget = p.token_budget

        self.last_injected = []
        mem_lines, bumped = [], []
        for h, rec in pairs:
            if h["dataset"] != "agent_memory_items" or len(mem_lines) >= p.max_memory:
                continue
            line = memory_line(rec)
            cost = p.tokenizer(line)
            if cost > budget:
                break
            mem_lines.append(line)
            budget -= cost
            bumped.append(rec)
            self.last_injected.append((h, rec))

        hist_lines = []
        for h, rec in pairs:
            if h["dataset"] not in _HISTORY_DATASETS or len(hist_lines) >= p.max_history \
                    or covered_by_boot(rec, h["dataset"], boot_min_seq):
                continue
            line = history_line(rec, h["dataset"])
            cost = p.tokenizer(line)
            if cost > budget:
                break
            hist_lines.append(line)
            budget -= cost

        msgs = frame_messages(user_text, mem_lines, hist_lines)
        if msgs:
            for rec in bumped:  # injected = recalled (§4.3)
                # ROI signal is best-effort, never costs the injection
                with contextlib.suppress(Exception):
                    self._memory.bump_access(rec["id"], rec.get("accessCount", 0))
        return msgs
