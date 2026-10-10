#!/usr/bin/env bash
# wait-synced.sh <addr> <space-id> <timeout-s>
#
# Polls GET /v1/spaces/<id>/sync-status until it reads "synced" three
# polls in a row (right after a write the counters can still show the
# pre-write state for a moment). Exits 1 on timeout.
set -euo pipefail
addr=$1 id=$2 timeout=$3
streak=0
for ((i = 0; i < timeout; i += 2)); do
  s=$(curl -fsS "$addr/v1/spaces/$id/sync-status" 2>/dev/null || true)
  if [ "$(jq -r '.state // empty' <<<"$s" 2>/dev/null)" = synced ]; then
    streak=$((streak + 1))
    if [ "$streak" -ge 3 ]; then
      echo "$id synced: $s"
      exit 0
    fi
  else
    streak=0
  fi
  sleep 2
done
echo "$id not synced after ${timeout}s: ${s:-no response}" >&2
exit 1
