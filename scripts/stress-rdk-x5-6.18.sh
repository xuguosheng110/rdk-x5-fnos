#!/usr/bin/env bash
set -uo pipefail

DURATION=${1:-1800}
OUT=${2:-/tmp/rdk-x5-linux-6.18-stress.log}
MOUNT=/mnt/x5-618-stress
PAYLOAD=$MOUNT/payload.bin
WORKERS=${WORKERS:-6}

mkdir -p "$MOUNT"
mountpoint -q "$MOUNT" || mount -t tmpfs -o size=768m tmpfs "$MOUNT"

cleanup() {
	jobs -pr | xargs -r kill 2>/dev/null || true
	wait 2>/dev/null || true
	rm -f "$PAYLOAD"
	umount "$MOUNT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec >>"$OUT" 2>&1
echo "START=$(date --iso-8601=seconds) DURATION=$DURATION WORKERS=$WORKERS"
echo "UNAME=$(uname -a)"
DMESG_LINES=$(dmesg | wc -l)

dd if=/dev/zero of="$PAYLOAD" bs=1M count=384 status=none
EXPECTED=$(sha256sum "$PAYLOAD" | awk '{print $1}')
echo "PAYLOAD_SHA256=$EXPECTED"

(
	end=$((SECONDS + DURATION))
	while (( SECONDS < end )); do
		printf 'SAMPLE=%s LOAD=' "$(date --iso-8601=seconds)"
		cut -d ' ' -f 1-3 /proc/loadavg
		for zone in /sys/class/thermal/thermal_zone*; do
			printf 'THERMAL=%s:%s:%s\n' "${zone##*/}" \
				"$(cat "$zone/type" 2>/dev/null)" \
				"$(cat "$zone/temp" 2>/dev/null)"
		done
		for policy in /sys/devices/system/cpu/cpufreq/policy*; do
			printf 'CPUFREQ=%s:%s\n' "${policy##*/}" \
				"$(cat "$policy/scaling_cur_freq" 2>/dev/null)"
		done
		sleep 30
	done
) &
MONITOR_PID=$!

for _ in $(seq 1 "$WORKERS"); do
	timeout "$DURATION" sh -c \
		'while :; do sha256sum "$1" >/dev/null || exit 1; done' sh "$PAYLOAD" &
done

wait
ACTUAL=$(sha256sum "$PAYLOAD" | awk '{print $1}')
echo "FINAL_SHA256=$ACTUAL"
if [[ $ACTUAL == "$EXPECTED" ]]; then
	echo "DATA_INTEGRITY=PASS"
else
	echo "DATA_INTEGRITY=FAIL"
fi
echo "END=$(date --iso-8601=seconds)"
echo "NEW_DMESG_BEGIN"
dmesg | tail -n "+$((DMESG_LINES + 1))"
echo "NEW_DMESG_END"
