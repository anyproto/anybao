#!/usr/bin/env bash
# last-deploy.sh <target> <repo>
#
# Prints the anybao commit the newest SUCCESSFUL deploy-repos run
# published to <target>'s (stable|nightly) <repo> (agent|connectors)
# space, or nothing when no run recorded one. The record is the run
# name — deploy-repos.yml sets `run-name` to
# "deploy-repos <target> <sha> <repos>" — read back through the runs
# API as display_title (needs `actions: read`; GH_TOKEN set). Runs
# older than the run-name line have the bare workflow name and are
# skipped: their checked-out sha is not in the API (head_sha is main's
# tip at dispatch time, not the sha any-ui bundled).
set -euo pipefail
target=$1 repo=$2
gh run list --workflow deploy-repos.yml --status success --limit 100 \
  --json displayTitle --jq '.[].displayTitle' |
  while read -r _ t sha repos; do
    [ "$t" = "$target" ] || continue
    [ "$repos" = both ] || [ "$repos" = "$repo" ] || continue
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || continue
    echo "$sha"
    break
  done
