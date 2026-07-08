"""anybao CLI — operational entry points.

`llm-seed` is the docs/llm-fixtures.md "one real call" step: run one
live `llm.chat` through the REAL transport and dump the raw provider
response to a fixture. The response carries no secrets; the request
(which does) is never written. After seeding, CI needs no API key —
adapter translation tests read the fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .llm import ADAPTERS, http_transport

FIXTURES_DIR = Path("harness/tests/fixtures")


def llm_seed(args: argparse.Namespace) -> int:
    key = os.environ.get(args.api_key_env, "")
    if not key:
        print(f"error: ${args.api_key_env} is not set", file=sys.stderr)
        return 2
    prov = {"provider": args.provider, "model": args.model,
            "base_url": args.base_url, "api_key_ref": args.api_key_env}
    adapter = ADAPTERS[args.provider]()
    req = adapter.build_request(
        [{"role": "user", "parts": [{"type": "text",
                                     "text": "Reply with the single word OK."}]}],
        "", [], args.model)
    raw = http_transport(os.environ.get)(prov, req)
    # sanity: the adapter must be able to translate what came back
    reply = adapter.parse_response(raw)
    assert reply["parts"], "provider response translated to no parts"

    out = Path(args.out or FIXTURES_DIR / f"llm_{args.provider}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=2) + "\n")
    print(f"seeded {out} (stop={reply['stop']}, "
          f"usage in/out={reply['usage']['in']}/{reply['usage']['out']})")
    return 0


_DEFAULTS = {
    "anthropic": ("claude-haiku-4-5-20251001", "https://api.anthropic.com",
                  "ANTHROPIC_API_KEY"),
    "openai-compat": ("gpt-4o-mini", "https://api.openai.com/v1",
                      "OPENAI_API_KEY"),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anybao")
    sub = p.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("llm-seed", help="record one real provider call "
                          "as a wire-shape fixture")
    seed.add_argument("--provider", choices=sorted(ADAPTERS), default="anthropic")
    seed.add_argument("--model")
    seed.add_argument("--base-url")
    seed.add_argument("--api-key-env")
    seed.add_argument("--out")
    seed.set_defaults(fn=llm_seed)

    args = p.parse_args(argv)
    if args.cmd == "llm-seed":
        model, base, env = _DEFAULTS[args.provider]
        args.model = args.model or model
        args.base_url = args.base_url or base
        args.api_key_env = args.api_key_env or env
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
