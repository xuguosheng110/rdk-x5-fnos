#!/bin/bash
# Runs as root from deploy_r3.command after one macOS administrator dialog.
set -u
args_file=${1:?missing args file}
args=()
while IFS= read -r -d '' item; do args+=("$item"); done < "$args_file"
repo=${args[0]:?missing repo root}
unset 'args[0]'
logdir="$repo/fnos/logs/uboot-diagnosis-20261106"
mkdir -p "$logdir"
log="$logdir/deploy-r3-$(date +%Y%m%d-%H%M%S).log"
{
  echo "DEPLOY_ROOT_START $(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "REPO=$repo"
  echo "ARGS=${args[*]}"
  if ! ssh -o ControlMaster=no -o ControlPath=/home/your-user/.cache/rdk-fnos-ssh/build-server.sock -o BatchMode=yes -o ConnectTimeout=8 guosheng@192.0.2.10 true; then
    echo "ROOT_SSH_MUX_UNAVAILABLE"
    exit 3
  fi
  cd "$repo"
  /usr/bin/env python3 -u "$repo/fnos/integration/hardware/deploy_r3.py" "${args[@]}" 2>&1
  rc=$?
  echo "DEPLOY_ROOT_RC=$rc"
  exit $rc
} | tee -a "$log"
exit ${PIPESTATUS[0]}
