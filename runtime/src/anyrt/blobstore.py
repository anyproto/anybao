"""BlobStore — the swappable backend for spilled trace blobs (ADR-001
§7, ADR-006 traceRef). Refs are content-addressed (`sha256:…`) so the
store swaps without touching the trace format. FileSidecarStore is the
dev/test tier; AnyFileStore (files-v2 attachments) is production (M4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def put(self, blobs: dict[str, str], base: Path) -> None: ...
    def load(self, base: Path) -> dict[str, str]: ...


class FileSidecarStore:
    """`<trace>.jsonl.blobs`, one {hash, data} JSON per line."""

    def _side(self, base: Path) -> Path:
        return base.with_suffix(base.suffix + ".blobs")

    def put(self, blobs: dict[str, str], base: Path) -> None:
        if not blobs:
            return
        import json

        self._side(base).write_text(
            "".join(
                json.dumps({"hash": h, "data": t}, sort_keys=True) + "\n"
                for h, t in sorted(blobs.items())
            )
        )

    def load(self, base: Path) -> dict[str, str]:
        import json

        side = self._side(base)
        if not side.exists():
            return {}
        out = {}
        for line in side.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                out[e["hash"]] = e["data"]
        return out
