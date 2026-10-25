"""The brain programs call only what any@v1 really exports. The program
tests run against a fake any@v1 that answers every name, so a call to a
method the module never exports passes them and fails the first real run
(`create_dataset` is a _Client method, not a module function). This test
loads the REAL any@v1 module under the kernel and checks every `_any.<name>`
the brain programs use."""

import ast
import json
from pathlib import Path

from kernelenv import load_kernel

PROGRAMS = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
BRAIN = sorted([*PROGRAMS.glob("brain*@v*.py"), *PROGRAMS.glob("brain*@v*/program.py")])


def _real_any():
    def effect(name, payload):
        if name == "runtime.get":
            if payload["key"] == "any.base_url":
                return {"value": "http://any"}
            raise KeyError(payload["key"])
        if name.startswith("http."):
            return {"status": 200, "headers": {}, "body": json.dumps({})}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=effect).use("any@v1")


def _calls_on(path, name):
    tree = ast.parse(path.read_text())
    return {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == name}


def _defined(path):
    tree = ast.parse(path.read_text())
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_every_any_call_in_the_brain_programs_is_exported():
    mod = _real_any()
    assert len(BRAIN) >= 6, BRAIN
    assert any(_calls_on(p, "_any") for p in BRAIN)
    for path in BRAIN:
        calls = _calls_on(path, "_any")
        missing = sorted(name for name in calls if not callable(getattr(mod, name, None)))
        name = path.parent.name if path.name == "program.py" else path.name
        assert missing == [], f"{name}: {missing}"


def test_every_kit_call_names_a_function_the_kit_defines():
    kit = PROGRAMS / "brainKit@v1.py"
    defined = _defined(kit)
    for path in BRAIN:
        missing = sorted(_calls_on(path, "kit") - defined)
        assert missing == [], f"{path.name}: {missing}"
