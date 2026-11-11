#!/bin/bash
# One macOS administrator authentication (system dialog), then one root
# process runs verified backup + verified r3 write/readback. No password is
# passed in argv or stored. UMS must already be active; no CP5 action here.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export FNOS_BOARD_LOCK_ROOT=/home/your-user/.local/state/fnos-lab
export FNOS_LAB_ENABLE_REAL_BACKENDS=1
MUX=/home/your-user/.cache/rdk-fnos-ssh/build-server.sock
if ! ssh -o ControlMaster=no -o "ControlPath=$MUX" -o BatchMode=yes -o ConnectTimeout=8 guosheng@192.0.2.10 true >/dev/null 2>&1; then
  echo "SSH ControlMaster unavailable; no administrator dialog opened" >&2
  exit 3
fi
if [[ $# -eq 0 ]]; then
  echo "usage: $0 --device /dev/diskN --image /path/r3.img --image-remote /server/r3.img --image-sha256 H --image-size N --acceptance FILE --reviewed-target FILE [--execute]" >&2
  exit 2
fi
tmpdir=$(mktemp -d /tmp/fnos-r3-deploy.XXXXXX)
trap 'rm -rf "$tmpdir"' EXIT
args_file="$tmpdir/args"
root_helper="$tmpdir/root.sh"
cp "$ROOT/fnos/integration/hardware/deploy_r3_root.sh" "$root_helper"
chmod 700 "$root_helper"
umask 077
{ printf '%s\0' "$ROOT"; printf '%s\0' "$@"; } > "$args_file"
# The paths are generated temp paths without whitespace; no user password is
# embedded, logged, or passed to the helper.
osascript <<APPLESCRIPT
with timeout of 14400 seconds
  do shell script "/bin/bash '$root_helper' '$args_file'" with administrator privileges
end timeout
APPLESCRIPT
