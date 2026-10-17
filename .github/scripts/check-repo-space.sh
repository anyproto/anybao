#!/usr/bin/env bash
# check-repo-space.sh <addr> <space-id>
#
# Refuses a deploy into a repo space whose harness shape is wrong:
# more than one `program` (or `agent_skill`) type, or two programs
# with the same name under one type. Either is what a deploy from a
# half-synced device leaves behind (see wait-synced.sh) — anyrt
# resolves programs by xKey and name, so a second copy is a coin
# toss at read time and a dead object otherwise.
set -euo pipefail
addr=$1 id=$2
types=$(curl -fsS "$addr/v1/spaces/$id/types?includeHidden=true")
fail=0
for xkey in program agent_skill; do
  n=$(jq -r --arg k "$xkey" '[.types[] | select(.xKey == $k)] | length' <<<"$types")
  if [ "$n" -gt 1 ]; then
    echo "space $id has $n types with xKey '$xkey':" >&2
    jq -r --arg k "$xkey" '.types[] | select(.xKey == $k) | "  \(.id)  \(.name)"' <<<"$types" >&2
    fail=1
  fi
done
objs=$(curl -fsS -X POST "$addr/v1/spaces/$id/objects/query" \
  -H 'content-type: application/json' -d '{"limit": 1000}')
dups=$(jq -r '[.records[] | select(.any.type != "__type__" and .any.name != null)
  | "\(.any.type) \(.any.name)"] | group_by(.) | map(select(length > 1) | .[0]) | .[]' <<<"$objs")
if [ -n "$dups" ]; then
  echo "space $id has duplicate objects (type name):" >&2
  echo "$dups" | sed 's/^/  /' >&2
  fail=1
fi
[ "$fail" = 0 ] && echo "$id: one type per xKey, no duplicate names"
exit "$fail"
