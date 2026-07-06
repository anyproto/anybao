"""anyrt — the isolated runtime anybao programs run against.

Core invariant: nothing executes side effects except through the effect
boundary. One invariant, two faces: bit-exact replay (deterministic
evaluation) and confinement (programs can do only what is allowed).

Contract docs: ../../docs/adr/ — no code lands ahead of its accepted ADR.
"""

__version__ = "0.0.1"
