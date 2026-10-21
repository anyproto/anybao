#!/usr/bin/env bash
# Set the anyrt version everywhere it is recorded: runtime/Cargo.toml,
# runtime/Cargo.lock, the generated licenses/RUST_DEPENDENCIES.md, and the
# license manifest that hashes them.
#
# Usage: tools/set_version.sh [--only-if-newer] <version>
#   <version>         X.Y.Z, a leading "v" is stripped (a tag works as is)
#   --only-if-newer   do nothing when the current version is the same or newer
#
# The license check runs BEFORE the change and must pass, so the manifest
# that this script records differs from the reviewed one only by the version.
# PYTHON overrides the interpreter (default: uv run --locked python).
set -euo pipefail

only_if_newer=false
if [ "${1:-}" = "--only-if-newer" ]; then
  only_if_newer=true
  shift
fi
version="${1:?usage: tools/set_version.sh [--only-if-newer] <version>}"
version="${version#v}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "not a X.Y.Z version: $version" >&2
  exit 1
}

cd "$(dirname "$0")/.."
read -r -a python <<<"${PYTHON:-uv run --locked python}"

current=$(perl -0ne 'print $1 if /\[package\]\nname = "anyrt"\nversion = "([^"]+)"/' runtime/Cargo.toml)
[ -n "$current" ] || {
  echo "no anyrt version in runtime/Cargo.toml" >&2
  exit 1
}
if [ "$current" = "$version" ]; then
  echo "anyrt version: $version (unchanged)"
  exit 0
fi
if $only_if_newer; then
  newest=$(printf '%s\n%s\n' "$current" "$version" | sort -V | tail -n1)
  if [ "$newest" = "$current" ]; then
    echo "anyrt version: $current (not older than $version, unchanged)"
    exit 0
  fi
fi

"${python[@]}" tools/check_licenses.py >/dev/null || {
  echo "license check fails before the version change; fix it first" >&2
  exit 1
}

# perl, not sed: the release builds on GNU and BSD runners, and sed -i differs.
perl -0pi -e 's/(\[package\]\nname = "anyrt"\nversion = )"[^"]+"/$1"'"$version"'"/' runtime/Cargo.toml
perl -0pi -e 's/(\[\[package\]\]\nname = "anyrt"\nversion = )"[^"]+"/$1"'"$version"'"/' runtime/Cargo.lock
perl -pi -e 's/^- anyrt \Q'"$current"'\E$/- anyrt '"$version"'/' licenses/RUST_DEPENDENCIES.md

for f in runtime/Cargo.toml runtime/Cargo.lock; do
  grep -A1 -x 'name = "anyrt"' "$f" | grep -qx "version = \"$version\"" || {
    echo "failed to set anyrt version $version in $f" >&2
    exit 1
  }
done
if grep -qx -- "- anyrt $current" licenses/RUST_DEPENDENCIES.md; then
  echo "failed to set anyrt version $version in licenses/RUST_DEPENDENCIES.md" >&2
  exit 1
fi

"${python[@]}" tools/check_licenses.py --record >/dev/null
echo "anyrt version: $current -> $version"
