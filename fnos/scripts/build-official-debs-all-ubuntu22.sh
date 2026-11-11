#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 SOURCE_DIR LOG_DIR" >&2
  exit 2
fi
if [[ $(id -u) -ne 0 ]]; then
  echo "Run with sudo to access Docker." >&2
  exit 3
fi

source_dir=$(cd "$1" && pwd)
mkdir -p "$2"
log_dir=$(cd "$2" && pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
build_context=$(cd "${script_dir}/../buildenv" && pwd)
output_uid=${SUDO_UID:-0}
output_gid=${SUDO_GID:-0}
toolchain=/opt/gcc-arm-11.2-2022.02-x86_64-aarch64-none-linux-gnu
image=fnos-x5-build:ubuntu22
sysroot=/work/deploy/rootfs_server

if [[ ! -d "${source_dir}/deploy/rootfs_server/usr/include" ]]; then
  echo "Missing server sysroot; run pack_image.sh once before building all debs." >&2
  exit 4
fi

docker build -t "$image" -f "${build_context}/Dockerfile.ubuntu22" "$build_context"
docker run --rm \
  --user "${output_uid}:${output_gid}" \
  -e HOME=/tmp/build-home \
  -e SYSROOT_DIR="$sysroot" \
  -v "${source_dir}:/work" \
  -v "${toolchain}:${toolchain}:ro" \
  -w /work \
  "$image" bash -lc 'mkdir -p "$HOME"; ./mk_debs.sh' \
  2>&1 | tee "${log_dir}/mk_debs-all.log"

