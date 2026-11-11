#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 SOURCE_DIR LOG_DIR" >&2
  exit 2
fi
if [[ $(id -u) -ne 0 ]]; then
  echo "Run with sudo to access Docker; outputs retain the invoking user's ownership." >&2
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
jobs=${JOBS:-8}

if [[ ! -x "${toolchain}/bin/aarch64-none-linux-gnu-gcc" ]]; then
  echo "Missing official toolchain at ${toolchain}." >&2
  exit 4
fi

docker build -t "$image" -f "${build_context}/Dockerfile.ubuntu22" "$build_context"
{
  date -u +'%Y-%m-%dT%H:%M:%SZ'
  docker image inspect "$image" --format 'build_image={{.Id}}'
  "${toolchain}/bin/aarch64-none-linux-gnu-gcc" --version | head -1
  git -C "$source_dir" rev-parse HEAD
  git -C "${source_dir}/source/kernel" rev-parse HEAD
} | tee "${log_dir}/kernel-build-environment.txt"

docker run --rm \
  --user "${output_uid}:${output_gid}" \
  -e HOME=/tmp/build-home \
  -e JOBS="$jobs" \
  -v "${source_dir}:/work" \
  -v "${toolchain}:${toolchain}:ro" \
  -w /work \
  "$image" bash -lc 'mkdir -p "$HOME"; ./mk_kernel.sh' \
  2>&1 | tee "${log_dir}/mk_kernel.log"
