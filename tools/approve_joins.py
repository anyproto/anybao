#!/usr/bin/env python3
"""approve_joins — auto-accept join requests on ALL of a server's spaces.

TEMPORARY WORKAROUND (2026-07-21, ADR-009 §8): until guest-key spaces
land, a repo account runs this against ITS any server to approve every
pending join request as a read-only member. The invite token carries no
permission — the grant happens here, at accept time, which is exactly
why the default is `reader` (view-only): joiners can read the repo's
programs/skills/kernel and never write.

Stdlib-only on purpose: it is ops tooling outside the runtime contract,
run next to the repo server, e.g.:

    python tools/approve_joins.py --addr http://127.0.0.1:7003
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def api(addr, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{addr}/v1{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def sweep(addr, permission):
    """One pass: every active space -> accept its pending requests."""
    accepted = 0
    spaces = api(addr, "GET", "/spaces").get("spaces", [])
    for sp in spaces:
        if sp.get("status") not in (None, "active"):
            continue
        sid = sp["id"]
        try:
            requests = api(addr, "GET",
                           f"/spaces/{sid}/members/requests").get("requests", [])
            for r in requests:
                api(addr, "POST", f"/spaces/{sid}/acl/accept",
                    {"requestRecordId": r["recordId"], "permission": permission})
                accepted += 1
                print(f"accepted {r.get('name') or r.get('identity')} "
                      f"into {sp.get('name') or sid} as {permission}", flush=True)
        except urllib.error.HTTPError as e:
            # not owner of this space / no ACL rights -> skip quietly-ish
            print(f"skip {sp.get('name') or sid}: HTTP {e.code}", file=sys.stderr)
    return accepted


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", default="http://127.0.0.1:7003",
                    help="the repo account's any server (default %(default)s)")
    ap.add_argument("--interval", type=float, default=3.0,
                    help="seconds between sweeps (default %(default)s)")
    ap.add_argument("--permission", default="reader",
                    choices=["reader", "guest", "writer", "admin"],
                    help="permission granted on accept (default %(default)s)")
    ap.add_argument("--once", action="store_true",
                    help="one sweep, then exit (for scripting/tests)")
    args = ap.parse_args()

    print(f"approve_joins: watching {args.addr} — accepting joiners as "
          f"{args.permission} (TEMPORARY until guest-key spaces)", flush=True)
    while True:
        try:
            sweep(args.addr, args.permission)
        except (urllib.error.URLError, OSError) as e:
            print(f"server unreachable: {e}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
