#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 ]]; then
  echo "usage: $0 KERNEL_SOURCE X5_HOBOT_DRIVERS INSTALL_ROOT" >&2
  exit 2
fi

kernel_source=$(realpath "$1")
driver_source=$(realpath "$2")
install_root=$(realpath "$3")
blob="$driver_source/bpu-hw_io/bpu_hw_io_x5.o_shipped"
work=$(mktemp -d "${TMPDIR:-/tmp}/x5-bpu-hw-io.XXXXXX")
trap 'rm -rf "$work"' EXIT

test -f "$kernel_source/Makefile"
test -f "$blob"

version=$(awk '$1 == "VERSION" && $2 == "=" {print $3}' "$kernel_source/Makefile")
patchlevel=$(awk '$1 == "PATCHLEVEL" && $2 == "=" {print $3}' "$kernel_source/Makefile")
[[ "$version.$patchlevel" == 6.18 ]] || {
  echo "kernel source is $version.$patchlevel; expected 6.18" >&2
  exit 1
}

# Kbuild's historical *_shipped copy rule does not resolve reliably for this
# legacy single-object external module. Work on a temporary copy so the pinned
# vendor checkout stays untouched.
install -m 0644 "$blob" "$work/bpu_hw_io_x5.o"
printf '%s\n' 'obj-m += bpu_hw_io_x5.o' >"$work/Makefile"

make -C "$kernel_source" ARCH=arm64 LLVM=1 M="$work" modules
make -C "$kernel_source" ARCH=arm64 LLVM=1 M="$work" \
  INSTALL_MOD_PATH="$install_root" INSTALL_MOD_STRIP=1 modules_install

module=$(find "$install_root/lib/modules" -name bpu_hw_io_x5.ko -type f -print -quit)
test -n "$module"
echo "installed: $module"
modinfo -F vermagic "$module"
