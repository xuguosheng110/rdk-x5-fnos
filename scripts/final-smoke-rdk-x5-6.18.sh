#!/usr/bin/env bash
set -uo pipefail

OUT=${1:-/root/rdk-x5-linux-6.18-final-smoke.log}
RC=0
exec > >(tee "$OUT") 2>&1

run_required() {
	printf '\n$ %s\n' "$*"
	"$@"
	local rc=$?
	printf '[exit=%d]\n' "$rc"
	if (( rc != 0 )); then
		RC=1
	fi
}

wait_active() {
	local unit=$1
	local remaining=${2:-60}

	while (( remaining > 0 )); do
		if systemctl is-active --quiet "$unit"; then
			return 0
		fi
		sleep 1
		((remaining--))
	done
	return 1
}

echo "START=$(date --iso-8601=seconds)"
run_required uname -a
run_required sh -c "sha256sum /boot/Image /boot/hobot/x5-rdk-v1p0.dtb"
run_required sh -c "test \"\$(sha256sum /boot/Image | awk '{print \$1}')\" = fec53d6a218e02cfcd42196b3cb510590e0623e1c4ee38c288130a7502352512"
run_required sh -c "test \"\$(sha256sum /boot/hobot/x5-rdk-v1p0.dtb | awk '{print \$1}')\" = 5427dd2c2c149c71ada2b258a36f5158abd890e229517895ae930aa02b6a67eb"
run_required wait_active x5-loadko.service 60
run_required wait_active cam-service.service 60
run_required sh -c "test \"\$(systemctl --failed --no-legend | wc -l)\" -eq 0"
run_required sh -c "test -c /dev/nano2d -a -c /dev/dri/card0 -a -c /dev/dri/renderD128 -a -c /dev/bpu"
run_required /root/nano2d-open-close-smoke
run_required timeout 10 speaker-test -D hw:0,0 -c 2 -r 48000 -F S16_LE -t sine -l 1
run_required arecord -D hw:0,0 -c 2 -r 48000 -f S16_LE -d 3 /tmp/final-capture.wav
run_required sh -c "test \"\$(stat -c %s /tmp/final-capture.wav)\" -gt 44"

ip link set can0 down 2>/dev/null || true
run_required ip link set can0 type can bitrate 500000 loopback on
run_required ip link set can0 up
run_required /root/can-loopback-smoke can0
ip link set can0 down 2>/dev/null || true
ip link set can0 type can loopback off 2>/dev/null || true

echo "TAINT=$(cat /proc/sys/kernel/tainted) (expected nonzero after approved proprietary BPU blob)"
echo "KERNEL_DIAGNOSTICS_BEGIN"
dmesg | grep -Ei 'BUG:|Oops:|panic|Call Trace:|lockup|stall|I/O error|DRM bridge corrupted|failed to stop' || true
echo "KERNEL_DIAGNOSTICS_END"
echo "END=$(date --iso-8601=seconds) RESULT=$([[ $RC == 0 ]] && echo PASS || echo FAIL)"
exit "$RC"
