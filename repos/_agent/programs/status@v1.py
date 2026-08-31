"""Your status line in the user's status bar — one short phrase of prose.

The status bar shows your presence (online / working) from serve's own
heartbeat; this module adds the human-readable WHAT ("migrating the
mail dataset, ~60% through the backlog") beside it. Set it when a run
will take more than a minute or two and update it as phases change —
silence is safe: the line decays 90s after the last set, falling back
to the run's title, so a forgotten update degrades to machine truth
instead of lying. Distinct from progress@v1 (per-job bars with counts);
this is the one-liner about YOU.
"""

# TRANSPORT (ADR-025 §3): the line crosses the effect boundary
# (`bao.status`) into serve state — serve, the SOLE publisher of
# `bao.status` beats, folds it into every beat and republishes
# immediately on set. The guest never publishes bus events directly.

__any_tool__ = True  # agent-callable (ADR-010 §4)


@span("status.set", kind="mutator")  # noqa: F821 - guest global
def set(line):
    """Set (or clear) your status line → {ok, line, at}.

    One short present-tense phrase of what you're doing right now —
    "reindexing the email corpus", not a log. Shown in the user's
    status bar beside your presence dot within a beat (~1s). Decays
    90s after the last set (update per phase, never per item);
    `set("")` clears it early. Serve-only: under `anyrt run` (no
    presence) this errors `not_configured` — skip status lines there.
    """
    return effect("bao.status", {"line": str(line or "")})  # noqa: F821
