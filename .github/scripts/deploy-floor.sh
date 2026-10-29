#!/usr/bin/env bash
# deploy-floor.sh <src> <ref> <prev>
#
# Prints the number of programs + skills under <src> (a repos/_<name>
# tree) that exist at BOTH commits <ref> (the one being deployed) and
# <prev> (the one the space was last deployed from). That is a count
# the space is known to hold already, so it is a sound floor for
# wait-synced.sh: counting <ref> alone deadlocks whenever a commit
# adds more programs than the README + type-row slack — the space can
# only reach that total after the deploy the wait is guarding
# (2026-10-29: run 40809575447, floor 21 vs 15 held, brain moved to
# _connectors). Program entries are the children of programs/ (a
# `name@v1/` dir or a `name@v1.py` file — the `.py` is stripped so a
# program that changed form still counts); skills are skills/*.md.
set -euo pipefail
src=$1 ref=$2 prev=$3
entries() { # <commit> — one normalised name per line, sorted
  {
    git ls-tree --name-only "$1:$src/programs" 2>/dev/null | sed -E 's/\.py$//' || true
    git ls-tree --name-only "$1:$src/skills" 2>/dev/null | grep -E '\.md$' || true
  } | sort -u
}
comm -12 <(entries "$ref") <(entries "$prev") | wc -l
