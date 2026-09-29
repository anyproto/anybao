"""The experimental overlay must not replace or mirror shared Bao sources."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "repos" / "_local_ai"


def test_overlay_contains_only_two_experimental_programs_and_no_skills():
    programs = {p.relative_to(LOCAL).as_posix() for p in LOCAL.rglob("*.py")}
    assert programs == {"programs/toolcaller@v2.py", "programs/llm@v2/program.py"}
    assert not (LOCAL / "skills").exists()


def test_shared_imports_are_explicit_and_local_import_stays_local():
    specs = set()
    for path in LOCAL.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "use" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                specs.add(node.args[0].value)
    assert specs == {"agent:any@v1", "agent:history@v1", "agent:autorecall@v1",
                     "agent:llm@v1", "llm@v2"}
