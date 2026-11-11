#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: sudo $0 DESTINATION" >&2
  exit 2
fi
if [[ $(id -u) -ne 0 ]]; then
  echo "Run with sudo so Docker and output ownership can be managed." >&2
  exit 3
fi

destination=$1
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
output_uid=${SUDO_UID:-0}
output_gid=${SUDO_GID:-0}

docker run --rm \
  -e OUTPUT_UID="$output_uid" \
  -e OUTPUT_GID="$output_gid" \
  -v "${destination}:/work" \
  -v "${script_dir}/sync-official-source.sh:/usr/local/bin/sync-official-source:ro" \
  ubuntu:22.04 bash -lc '
    set -euo pipefail
    trap '\''chown -R "${OUTPUT_UID}:${OUTPUT_GID}" /work'\'' EXIT
    export DEBIAN_FRONTEND=noninteractive
    sed -i \
      -e "s|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g" \
      -e "s|http://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g" \
      /etc/apt/sources.list
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates git repo openssh-client
    /usr/local/bin/sync-official-source /work
  '
