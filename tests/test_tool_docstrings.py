"""ADR-010 §1: a tool method's docstring opens with ONE summary line — the
only line listings (`## Tools`, help(mod)) show — then a blank line and
the body. A summary wrapped onto a second line renders cut mid-sentence;
a missing docstring renders nothing. Checked on the LOADED modules under
the real kernel, so generated surfaces (any@v1's @_public export) count
exactly as describe() sees them; unlisted plumbing is exempt."""

import inspect
import types
from pathlib import Path

import pytest
from kernelenv import load_kernel, local_source

ROOT = Path(__file__).resolve().parents[1]
REPOS = {"agent": ROOT / "repos" / "_agent" / "programs",
         "connectors": ROOT / "repos" / "_connectors" / "programs"}
SUMMARY_MAX = 100


def _tool_programs():
    for alias, root in REPOS.items():
        for p in sorted(root.iterdir()):
            src = p / "program.py" if p.is_dir() else p
            if src.suffix == ".py" and src.exists() and "__any_tool__ = True" in src.read_text():
                spec = p.name.removesuffix(".py")
                yield pytest.param(alias, spec, id=f"{alias}:{spec}")


def _cross_repo(spec):
    """Alias-qualified specs resolve in their repo; a bare spec a module
    of the OTHER repo imports (progress@v1 → any@v1, resolved in its
    defining space at runtime) falls back to the agent repo — the hook
    does not see the importing module."""
    alias, _, name = spec.rpartition(":")
    if alias in REPOS:
        return local_source(name, REPOS[alias])
    for root in REPOS.values():
        if (root / f"{name}.py").exists() or (root / name / "program.py").exists():
            return local_source(name, root)
    return None


def _listed_functions(mod):
    for n, f in vars(mod).items():
        if (isinstance(f, types.FunctionType) and f.__module__ == mod.__name__
                and not n.startswith("_") and n != "main"
                and getattr(f, "__any_listed__", True) is not False):
            yield n, f


@pytest.mark.parametrize("alias,spec", list(_tool_programs()))
def test_every_listed_method_opens_with_one_summary_line(alias, spec):
    def effect(name, payload):
        raise AssertionError(f"module load ran effect {name}")

    app = load_kernel(effect=effect, programs_dir=REPOS[alias], module_source=_cross_repo)
    mod = app.use(spec)
    bad = []
    for n, f in _listed_functions(mod):
        lines = (inspect.getdoc(f) or "").split("\n")
        if not lines[0].strip():
            bad.append(f"{n}: no docstring")
        elif len(lines) > 1 and lines[1].strip():
            bad.append(f"{n}: summary wraps onto a second line — {lines[0]!r}")
        elif len(lines[0]) > SUMMARY_MAX:
            bad.append(f"{n}: summary longer than {SUMMARY_MAX} chars")
    assert not bad, f"{spec}:\n  " + "\n  ".join(bad)
