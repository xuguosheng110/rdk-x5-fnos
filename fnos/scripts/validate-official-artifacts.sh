#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 SOURCE_DIR" >&2
  exit 2
fi

source_dir=$(cd "$1" && pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
test_module=$(cd "${script_dir}/../tests/header-smoke" && pwd)
deploy="${source_dir}/deploy"
kernel="${deploy}/kernel"
release=6.1.83
toolchain=/opt/gcc-arm-11.2-2022.02-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-
toolchain_sysroot=$("${toolchain}gcc" -print-sysroot)
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT

boot_deb=$(find "${deploy}/deb_pkgs" -maxdepth 1 -name 'hobot-boot_*.deb' -print -quit)
dtb_deb=$(find "${deploy}/deb_pkgs" -maxdepth 1 -name 'hobot-dtb_*.deb' -print -quit)
headers_deb=$(find "${deploy}/deb_pkgs" -maxdepth 1 -name 'hobot-kernel-headers_*.deb' -print -quit)
for package in "$boot_deb" "$dtb_deb" "$headers_deb"; do
  [[ -n "$package" && -s "$package" ]] || { echo "Missing critical deb." >&2; exit 3; }
  dpkg-deb --info "$package" >/dev/null
  dpkg-deb -f "$package" Package Version Architecture Depends
done

dpkg-deb -x "$boot_deb" "${tmp_dir}/boot"
dpkg-deb -x "$dtb_deb" "${tmp_dir}/dtb"
dpkg-deb -x "$headers_deb" "${tmp_dir}/headers"
cmp "$kernel/Image" "${tmp_dir}/boot/boot/Image"
cmp "$kernel/dtb/x5-rdk-v1p0.dtb" "${tmp_dir}/dtb/boot/hobot/x5-rdk-v1p0.dtb"
test -f "${tmp_dir}/headers/usr/src/linux-headers-${release}/Module.symvers"
test ! -e "${tmp_dir}/headers/usr/src/linux-headers-6"
header_tools=(
  scripts/selinux/genheaders/genheaders scripts/selinux/mdp/mdp
  scripts/kallsyms scripts/asn1_compiler scripts/mod/mk_elfconfig
  scripts/mod/modpost scripts/dtc/fdtoverlay scripts/dtc/dtc
  scripts/sorttable scripts/kconfig/conf scripts/basic/fixdep
)
for tool in "${header_tools[@]}"; do
  readelf -h "${tmp_dir}/headers/usr/src/linux-headers-${release}/${tool}" \
    | grep -q 'Machine:.*AArch64'
done

module_count=$(find "$kernel/modules/lib/modules/${release}" -name '*.ko' | wc -l)
vermagic=$(find "$kernel/modules/lib/modules/${release}" -name '*.ko' -print0 \
  | xargs -0 -n1 modinfo -F vermagic | sort -u)
[[ "$vermagic" == "$release SMP preempt mod_unload aarch64" ]]
depmod_output=$(depmod -ae -F "${source_dir}/source/kernel/System.map" \
  -b "$kernel/modules" "$release" 2>&1)
[[ -z "$depmod_output" ]] || { printf '%s\n' "$depmod_output" >&2; exit 4; }

cp -a "$test_module" "${tmp_dir}/header-smoke"
QEMU_LD_PREFIX="$toolchain_sysroot" make -s \
  -C "${tmp_dir}/headers/usr/src/linux-headers-${release}" \
  M="${tmp_dir}/header-smoke" ARCH=arm64 CROSS_COMPILE="$toolchain" modules
[[ $(modinfo -F vermagic "${tmp_dir}/header-smoke/hello.ko") == "$vermagic" ]]
readelf -h "${tmp_dir}/header-smoke/hello.ko" | grep -q 'Machine:.*AArch64'

fdtget "$kernel/dtb/x5-rdk-v1p0.dtb" / compatible | grep -qx 'D-Robotics, x5'
strings "$kernel/Image" >"${tmp_dir}/image.strings"
grep -m1 "Linux version ${release}" "${tmp_dir}/image.strings"

find "$deploy" -maxdepth 2 -type f \( -name '*.img' -o -name '*.deb' -o -name Image \) \
  -print0 | sort -z | xargs -0 sha256sum >"${deploy}/fnos-artifacts.sha256"
printf 'dtb_count=%s\n' "$(find "$kernel/dtb" -maxdepth 1 -name '*.dtb' | wc -l)"
printf 'module_count=%s\n' "$module_count"
printf 'vermagic=%s\n' "$vermagic"
printf 'headers_smoke=PASS\n'
printf 'header_arm64_tools=%s\n' "${#header_tools[@]}"
printf 'dtb_compatible=PASS\n'
printf 'depmod_unresolved=0\n'
