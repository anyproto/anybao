#!/bin/sh
# ADR-032 §3 plumbing gate: an aarch64-apple-ios-sim anyrt (iOS engine
# knobs, in-place loader) runs `kernel check` in a booted simulator.
# Not the §3 proof — the simulator has no signed-code constraint; the
# hardened macOS check is (tools/hardened_kernel_check.sh).
#   tools/ios_sim_kernel_check.sh runtime/target/ios-sim/aarch64-apple-ios-sim/release/anyrt
set -eu
bin=$(cd "$(dirname "${1:?usage: $0 <ios-sim anyrt>}")" && pwd)/$(basename "$1")
dev=$(xcrun simctl list devices available | sed -nE 's/^ +iPhone[^(]*\(([0-9A-F-]{36})\).*/\1/p' | head -1)
[ -n "$dev" ] || { echo "no iPhone simulator available"; exit 1; }
booted_here=0
if ! xcrun simctl list devices booted | grep -q "$dev"; then
  xcrun simctl boot "$dev"
  booted_here=1
fi
trap '[ "$booted_here" = 1 ] && xcrun simctl shutdown "$dev"' EXIT
xcrun simctl bootstatus "$dev" -b >/dev/null
codesign -f -s - "$bin"
out=$(xcrun simctl spawn "$dev" "$bin" kernel check)
echo "$out"
echo "$out" | grep -q '"status":"ok"'
echo "$out" | grep -q '"precompiled":true'
