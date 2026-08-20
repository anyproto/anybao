"""The connectors-repo shim over anybao's kernelenv: a kernel whose
local module resolution roots at THIS repo's programs/. (Deliberately
NOT a conftest.py — that module name collides with the root suite's
conftest under pytest's rootdir import mode.)"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_ANYBAO_TESTS = str(HERE.parents[2] / "tests")
if _ANYBAO_TESTS not in sys.path:
    sys.path.insert(0, _ANYBAO_TESTS)

import kernelenv  # noqa: E402

PROGRAMS_DIR = HERE.parent / "programs"
_AGENT_PROGRAMS = HERE.parents[2] / "repos" / "_agent" / "programs"


def _agent_repo_source(spec):
    """Cross-repo deps (`agent:<name>@vN`, ADR-009) resolve to the REAL
    agent-repo source — except any@v1/llm@v1, which kernelenv shims to
    the test fakes before this hook is consulted."""
    if spec.startswith("agent:"):
        return kernelenv.local_source(spec.split(":")[-1], _AGENT_PROGRAMS)
    return None


def connector_kernel(effect=None, any_client=None, llm_chat=None):
    return kernelenv.load_kernel(effect=effect, any_client=any_client,
                                 llm_chat=llm_chat,
                                 programs_dir=PROGRAMS_DIR,
                                 module_source=_agent_repo_source)
