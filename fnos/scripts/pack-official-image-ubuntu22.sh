#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: sudo $0 SOURCE_DIR LOG_DIR [download|local]" >&2
  exit 2
fi
if [[ $(id -u) -ne 0 ]]; then
  echo "Run with sudo; the official packer needs loop, mount, and chroot." >&2
  exit 3
fi

source_dir=$(cd "$1" && pwd)
mkdir -p "$2"
log_dir=$(cd "$2" && pwd)
mode=${3:-download}
case "$mode" in
  download) local_arg=() ;;
  local) local_arg=(-l) ;;
  *) echo "Mode must be download or local." >&2; exit 4 ;;
esac

output_uid=${SUDO_UID:-0}
output_gid=${SUDO_GID:-0}
image=fnos-x5-build:ubuntu22
config=build_params/ubuntu-22.04_server_rdk-x5_release.conf

docker run --rm --privileged \
  -e OUTPUT_UID="$output_uid" \
  -e OUTPUT_GID="$output_gid" \
  -v /dev:/dev \
  -v "${source_dir}:/work" \
  -w /work \
  "$image" bash -lc '
    set -euo pipefail
    restore_owner() {
      for path in deploy rootfs_server deb_packages third_packages; do
        [[ ! -e "$path" ]] || chown -R "${OUTPUT_UID}:${OUTPUT_GID}" "$path"
      done
    }
    trap restore_owner EXIT
    ./pack_image.sh "$@"
  ' _ -c "$config" "${local_arg[@]}" \
  2>&1 | tee "${log_dir}/pack-image-${mode}.log"
