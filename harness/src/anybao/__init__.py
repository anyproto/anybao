"""anybao — the agent harness (app layer over anyrt).

Space programs stay big-but-thin (policy + orchestration); mechanics live
here, tested. Layering: programs -> kernel API -> anybao -> anyrt -> any HTTP.

Contract docs: ../../docs/adr/ — no code lands ahead of its accepted ADR.
"""

__version__ = "0.0.1"
