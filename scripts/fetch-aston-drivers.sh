#!/usr/bin/env bash
# Clones the driver checkouts of drivers/submodules.lock at their pinned commits (Linux build host only).
# usage: scripts/fetch-aston-drivers.sh [device-repository]
set -euo pipefail
DEV=$(cd "${1:-$(dirname "$0")/..}" && pwd)
grep -v '^\s*#' "$DEV/drivers/submodules.lock" | while read -r path url commit; do
  [ -n "${path:-}" ] || continue
  dir="$DEV/$path"
  if [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)" = "$commit" ]; then
    echo "ok    $path $commit"
    continue
  fi
  rm -rf "$dir"
  git init -q "$dir"
  git -C "$dir" remote add origin "$url"
  git -C "$dir" fetch -q --depth 1 origin "$commit"
  git -C "$dir" checkout -q FETCH_HEAD
  git -C "$dir" submodule update -q --init --recursive --depth 1
  echo "fetch $path $(git -C "$dir" rev-parse HEAD)"
done
apply_driver_patch() {
  local dir="$1" patch="$2"
  if git -C "$dir" apply -R --check "$patch" 2>/dev/null; then
    echo "patch ok $(basename "$dir") $(basename "$patch")"
    return 0
  fi
  git -C "$dir" apply --check "$patch"
  git -C "$dir" apply "$patch"
  echo "patch applied $(basename "$dir") $(basename "$patch")"
}
apply_driver_patch "$DEV/drivers/QcomDwc3" "$DEV/patches/drivers/QcomDwc3-aston-usb2-smmu.patch"
apply_driver_patch "$DEV/drivers/ShengHidInput" "$DEV/patches/drivers/ShengHidInput-touch-split.patch"
# Ath12kWifi's own source fetch needs pattern-mode sparse checkouts (scripts/prefetch-ath12k-sources.py).
python3 "$DEV/scripts/prefetch-ath12k-sources.py" "$DEV/drivers/Ath12kWifi"
