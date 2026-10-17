#!/usr/bin/env bash
# wait-synced.sh <addr> <space-id> <timeout-s> [min-total]
#
# Polls GET /v1/spaces/<id>/sync-status until it reads "synced" three
# polls in a row with an unchanged `total` (right after a write the
# counters can still show the pre-write state for a moment). Exits 1
# on timeout.
#
# min-total: a floor on `total`. A FRESH device reports "synced" for
# the handful of objects it has received so far, before the space's
# tree has arrived (2026-10-12: deploy-repos runs 38195322072 and
# 38244491720 read "synced 4/4" and "synced 17/17" on a 60-object
# space, then re-created a second Program type and a duplicate
# program). Bridge until sync-status distinguishes "caught up" from
# "nothing pending yet" upstream (any server; ticket pending).
set -euo pipefail
addr=$1 id=$2 timeout=$3 min_total=${4:-0}
streak=0 last_total=
for ((i = 0; i < timeout; i += 2)); do
  s=$(curl -fsS "$addr/v1/spaces/$id/sync-status" 2>/dev/null || true)
  state=$(jq -r '.state // empty' <<<"$s" 2>/dev/null || true)
  total=$(jq -r '.total // 0' <<<"$s" 2>/dev/null || echo 0)
  if [ "$state" = synced ] && [ "$total" -ge "$min_total" ] && [ "$total" = "${last_total:-$total}" ]; then
    streak=$((streak + 1))
    if [ "$streak" -ge 3 ]; then
      echo "$id synced: $s"
      exit 0
    fi
  else
    streak=0
  fi
  last_total=$total
  sleep 2
done
echo "$id not synced after ${timeout}s (min total $min_total): ${s:-no response}" >&2
exit 1
