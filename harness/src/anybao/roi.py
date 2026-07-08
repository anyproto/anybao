"""Live ROI metrics — ADR-007 §1b/§5: memory features earn their keep
by measurement, not assumption. Two rates tune or kill the write path:

- **extracted-but-unrecalled**: extraction-sourced items whose
  accessCount never left 0 (the §1a failure shape — 46/55 in the audit).
- **injected-but-unreferenced**: auto-recalled items the model never
  visibly used in its replies (cheap significant-word heuristic — a
  drift signal, not a verdict).

Injections log to a plain `agent_roi_injections` dataset on the brain
object (the hit's objectId), one record per injected item per turn.
"""

from __future__ import annotations

import re

from .anyclient import AnyClient

DATASET = "agent_roi_injections"
_WORD = re.compile(r"[a-zA-Z0-9]{5,}")


def referenced(context: str, replies: list[str]) -> bool:
    """Did any significant context word surface in the replies?"""
    text = " ".join(replies).lower()
    return any(w.lower() in text for w in _WORD.findall(context or ""))


def log_injection(client: AnyClient, space: str,
                  injected: list[tuple[dict, dict]], replies: list[str],
                  *, ts: int) -> int:
    """One record per injected (hit, item) pair; the brain object comes
    from the hit pointer. Returns the number of records written."""
    n = 0
    for hit, item in injected:
        client.upsert_record(space, hit["objectId"], DATASET,
                             f"{item['id']}:{ts}",
                             {"itemId": item["id"], "ts": ts,
                              "referenced": referenced(item.get("context", ""),
                                                       replies)})
        n += 1
    return n


def injection_stats(client: AnyClient, space: str, brain_id: str) -> dict:
    rows = client.query(space, brain_id, DATASET)
    injected = len(rows)
    used = sum(1 for r in rows if r.get("referenced"))
    return {"injected": injected, "referenced": used,
            "unreferencedRate": (injected - used) / injected if injected else 0.0}


def extraction_stats(client: AnyClient, space: str, brain_id: str) -> dict:
    items = client.query(space, brain_id, "agent_memory_items",
                         filter={"source": "extraction"})
    extracted = len(items)
    recalled = sum(1 for i in items if i.get("accessCount", 0) > 0)
    return {"extracted": extracted, "recalled": recalled,
            "unrecalledRate": (extracted - recalled) / extracted if extracted else 0.0}
