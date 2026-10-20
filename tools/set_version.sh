#!/usr/bin/env bash
# Set the anyrt crate version in runtime/Cargo.toml and runtime/Cargo.lock.
# Usage: tools/set_version.sh 0.9.0   (a leading "v" is stripped)
set -euo pipefail

version="${1:?usage: tools/set_version.sh <version>}"
version="${version#v}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "not a X.Y.Z version: $version" >&2
  exit 1
}

cd "$(dirname "$0")/../runtime"
# perl, not sed: the build runs on GNU and BSD runners, and sed -i differs.
perl -0pi -e 's/(\[package\]\nname = "anyrt"\nversion = )"[^"]+"/$1"'"$version"'"/' Cargo.toml
perl -0pi -e 's/(\[\[package\]\]\nname = "anyrt"\nversion = )"[^"]+"/$1"'"$version"'"/' Cargo.lock

for f in Cargo.toml Cargo.lock; do
  grep -A1 -x 'name = "anyrt"' "$f" | grep -qx "version = \"$version\"" || {
    echo "failed to set anyrt version $version in runtime/$f" >&2
    exit 1
  }
done
echo "anyrt version: $version"
