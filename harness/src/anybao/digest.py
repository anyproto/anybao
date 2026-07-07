"""Digest — ADR-005 §4: render a CellResult into ToolResult content.

Progressive disclosure (the 4k successor): budget-aware inline in
TOKENS not chars; over budget → stub with size/schema/selector; the
full value stays in the guest value store (`values.get`). Sections:
Output → Last value → Side effects. Orientation summaries and the
effect-mutation any:// links are HOOKS wired when llm/anyclient exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from anyrt import trace as tr
from anyrt.executor import CellResult, ValueRef


def approx_tokens(text: str) -> int:
    """Cheap proxy until a real tokenizer is wired (M2 metrics tune it).
    ~4 chars/token is the standard rule of thumb."""
    return (len(text) + 3) // 4


@dataclass
class DigestPolicy:
    inline_token_budget: int = 1000     # per value (ADR-005 §4)
    max_side_effect_lines: int = 12
    tokenizer = staticmethod(approx_tokens)


def _render_value(cell_id: str, ref: ValueRef, i: int | str, policy: DigestPolicy) -> str:
    if policy.tokenizer(ref.repr) <= policy.inline_token_budget:
        return ref.repr
    sel = f'values.get("{cell_id}", {i!r})' if i != "last" else f'values.get("{cell_id}", "last")'
    return f"[{ref.size} bytes, {ref.schema} — {sel} to walk]"


def _side_effects(records: list[dict], cell_id: str, policy: DigestPolicy) -> str:
    effs = [
        r for r in records
        if r["kind"] == "effect" and r.get("cell") == cell_id
        and r["effect"] not in ("trace.effects_of", "trace.effect_get")
    ]
    if not effs:
        return ""
    counts: dict[str, int] = {}
    mutations: list[dict] = []
    for r in effs:
        counts[r["effect"]] = counts.get(r["effect"], 0) + 1
        if r.get("meta", {}).get("class") == "mutate":
            mutations.append(r)
    lines = [f"{name} ×{n}" for name, n in sorted(counts.items())]
    # mutations called out individually (any:// links are a later hook)
    for m in mutations[: policy.max_side_effect_lines]:
        lines.append(f"  mutate {m['effect']} #{m['seq']}")
    return "Side effects: " + ", ".join(lines[: policy.max_side_effect_lines]) + \
        (f'  (full: effects.of("{cell_id}"))' if effs else "")


def render(
    result: CellResult,
    records: list[dict],
    *,
    policy: DigestPolicy | None = None,
    hints: list[str] | None = None,
) -> str:
    policy = policy or DigestPolicy()
    parts: list[str] = []

    if result.prints:
        rendered = [
            f"#{i} {_render_value(result.cell_id, p, i, policy)}"
            for i, p in enumerate(result.prints)
        ]
        parts.append("Output:\n" + "\n".join(rendered))

    if result.last_value is not None:
        parts.append(
            "Last value: " + _render_value(result.cell_id, result.last_value, "last", policy)
        )

    se = _side_effects(records, result.cell_id, policy)
    if se:
        parts.append(se)

    if result.error:
        tb = f"\n{result.error.traceback_str}" if result.error.traceback_str else ""
        parts.append(f"Error: {result.error.type}: {result.error.message}{tb}")

    if result.interrupted and not result.error:
        parts.append("(cell interrupted)")

    for h in hints or []:
        parts.append(h)

    return "\n\n".join(parts) or "(no output)"


def teaching_hints(result: CellResult, records: list[dict]) -> list[str]:
    """Just-in-time nudges (ADR-002 *_many, ADR-005 §4) — emitted only
    when triggered, never standing prompt."""
    hints: list[str] = []
    counts: dict[str, int] = {}
    for r in tr.call_trace(records, result.cell_id):
        if r["kind"] == "effect":
            counts[r["effect"]] = counts.get(r["effect"], 0) + 1
    for name, n in counts.items():
        if n >= 4 and "." in name:
            base = name.rsplit(".", 1)[0]
            hints.append(
                f"hint: {n}× sequential {name} — use {base}.{name.split('.')[-1]}_many(...) "
                f"for one round-trip"
            )
    return hints
