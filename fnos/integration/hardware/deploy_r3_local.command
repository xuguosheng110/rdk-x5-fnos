#!/bin/bash
# RDK X5 fnOS r3 — ONE foreground entry: verified whole-card backup -> write -> full readback.
# Double-click in Finder (opens Terminal) and type the macOS administrator password once.
# It never powers the board, never exits UMS and never boots. CP5/serial boot is a separate step.
set -euo pipefail

ROOT="/path/to/rdk-x5-fnos"
STAGE="$ROOT/fnos/logs/fnos-x5-candidate-r3-20261106/deploy-local"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$STAGE/terminal-$TS.log"
mkdir -p "$STAGE"

echo "=============================================================="
echo " RDK X5 fnOS r3 deployment (backup -> write -> readback)"
echo " Target : /dev/disk12  (UMS disk 0, 31,914,983,424 bytes)"
echo " Image  : fnos-x5-candidate-r3.img  13,157,531,648 bytes"
echo " Backup : /Volumes/data/fnos-backups/20261106-sd/"
echo " This WILL overwrite the SD card currently exported by U-Boot."
echo "=============================================================="

exec /usr/bin/sudo -E /bin/bash -s -- "$ROOT" "$STAGE" "$LOG" <<'ROOTSCRIPT'
set -euo pipefail
ROOT=$1
STAGE=$2
LOG=$3
export FNOS_LAB_ENABLE_REAL_BACKENDS=1
export FNOS_BOARD_LOCK_ROOT=/home/your-user/.local/state/fnos-lab
exec > >(tee -a "$LOG") 2>&1

echo "START $(date '+%Y-%m-%dT%H:%M:%S%z')"

# Keep the Mac awake for the whole multi-hour read/write/readback.
/usr/bin/caffeinate -dimsu -w $$ &

PROBE=$(/usr/bin/mktemp "$STAGE/probe-512.XXXXXX")
if ! /bin/dd if=/dev/rdisk12 of="$PROBE" bs=512 count=1 2>"$PROBE.stderr"; then
  echo "PROBE_FAILED"; /bin/cat "$PROBE.stderr"; exit 10
fi
BYTES=$(/usr/bin/wc -c < "$PROBE" | /usr/bin/tr -d ' ')
[[ "$BYTES" == "512" ]] || { echo "PROBE_SHORT bytes=$BYTES"; exit 11; }
echo "PROBE_OK bytes=512 sha256=$(/usr/bin/shasum -a 256 "$PROBE" | /usr/bin/awk '{print $1}')"

/opt/homebrew/bin/python3 -u "$ROOT/fnos/integration/hardware/deploy_r3_local.py" \
  --device /dev/disk12 \
  --image "/home/your-user/.cache/rdk-fnos-deploy/fnos-x5-candidate-r3.img" \
  --image-sha256 653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599 \
  --image-size 13157531648 \
  --acceptance "$ROOT/fnos/coordination/codex-candidate-r3-acceptance-20261106.json" \
  --reviewed-target "$ROOT/fnos/coordination/codex-ums-reviewed-target-20261106.json" \
  --backup-dir /Volumes/data/fnos-backups/20261106-sd \
  --evidence-dir "$STAGE" \
  --execute
RC=$?
echo "DEPLOY_RC=$RC"
exit $RC
ROOTSCRIPT

RC=$?
echo
if [[ $RC -eq 0 ]]; then
  echo "DEPLOY PASS — backup, write and full readback all verified."
  echo "Next: run the reviewed serial/CP5 boot entry (boot_r3.py)."
else
  echo "DEPLOY FAILED rc=$RC — see log: $LOG"
fi
echo "Log: $LOG"
read -r -p "Press Enter to close this window..." _
exit $RC
