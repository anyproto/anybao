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


def test_prompt_and_digest_helpers_match_the_upstream_loop():
    names = {"_tool_listing", "_tool_docs", "_skill_index", "_compose_skills",
             "_load_system_skills", "compose_system_parts", "compose_system",
             "_render_value", "render_digest", "_teach", "_context_suffix",
             "_prompt_floor", "_count_effects"}

    def helpers(path):
        return {node.name: ast.dump(node, include_attributes=False)
                for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name in names}

    upstream = helpers(ROOT / "repos/_agent/programs/toolcaller@v1.py")
    local = helpers(LOCAL / "programs/toolcaller@v2.py")
    assert upstream.keys() == names
    assert local == upstream
