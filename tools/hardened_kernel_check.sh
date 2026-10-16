#!/bin/sh
# ADR-032 §3 proof on macOS: a precompiled-kernel anyrt, signed with the
# hardened runtime and NO entitlements (so no JIT), runs `kernel check`
# through the in-place loader, and no writable+executable or anonymous
# executable region appears in the live process.
#   tools/hardened_kernel_check.sh runtime/target/precompiled/release/anyrt
set -eu
src=${1:?usage: $0 <precompiled-kernel anyrt>}
work=$(mktemp -d)
bin="$work/anyrt"
cp "$src" "$bin"
codesign -f -s - --options runtime "$bin"
codesign -dv "$bin" 2>&1 | grep -q 'flags=.*runtime' || { echo "not hardened"; exit 1; }
if codesign -d --entitlements - "$bin" 2>/dev/null | grep -q 'allow-jit\|allow-unsigned-executable-memory'; then
  echo "unexpected JIT entitlement"; exit 1
fi
"$bin" kernel check --runs 40 >"$work/runs" &
pid=$!
sleep 2
vmmap -wide "$pid" >"$work/vmmap" 2>&1 || true
wait "$pid"
runs=$(grep -c '"status":"ok"' "$work/runs")
fuels=$(sed -n 's/.*"fuelUsed":\([0-9]*\).*/\1/p' "$work/runs" | sort -u | wc -l | tr -d ' ')
echo "kernel check: $runs/40 ok, $fuels distinct fuel value(s)"
[ "$runs" = 40 ] && [ "$fuels" = 1 ] || exit 1
grep -q '__TEXT' "$work/vmmap" || { echo "vmmap captured nothing"; cat "$work/vmmap"; exit 1; }
# current protection (before the slash) holding both w and x
wx=$(grep -E ' [r-]wx/' "$work/vmmap" || true)
# executable regions that no file on disk backs (JIT'd or copied code)
anon=$(grep -E ' [r-]-x/' "$work/vmmap" | grep -v ' /' || true)
if [ -n "$wx$anon" ]; then
  echo "writable+executable or anonymous executable regions:"; echo "$wx$anon"; exit 1
fi
echo "no writable+executable or anonymous executable region"
