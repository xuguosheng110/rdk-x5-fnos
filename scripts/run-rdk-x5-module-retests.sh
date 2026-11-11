#!/usr/bin/env bash
# Focused reruns for cases that need corrected setup or finer-grained evidence.
set -uo pipefail

out=${1:-/tmp/rdk-x5-module-retests.log}
work=/tmp/rdk-x5-module-retests
export work
pass=0
fail=0
exec > >(tee -a "$out") 2>&1

run() {
    local id=$1
    local command=$2
    printf '\n===== %s =====\n$ %s\n' "$id" "$command"
    bash -e -o pipefail -c "$command"
    local rc=$?
    printf '[CASE %s RC=%d]\n' "$id" "$rc"
    if (( rc == 0 )); then pass=$((pass + 1)); else fail=$((fail + 1)); fi
    return 0
}

mkdir -p "$work"
run BASE-01 'date --iso-8601=seconds; uname -a; cat /proc/sys/kernel/tainted; systemctl --failed --no-pager || true'
run CAN-CLASSIC-125K 'ip link set can0 down; ip link set can0 type can bitrate 125000 loopback on; ip link set can0 up; f=$(mktemp); timeout 4 candump -L can0 >"$f" & p=$!; sleep 1; cansend can0 123#DEADBEEF; wait "$p" || test $? = 124; cat "$f"; grep -qi "123.*DEADBEEF" "$f"; rc=$?; ip link set can0 down; unlink "$f"; exit "$rc"'
run CAN-FD-2M 'ip link set can0 down; ip link set can0 type can bitrate 500000 dbitrate 2000000 fd on loopback on; ip link set can0 up; f=$(mktemp); timeout 4 candump -L can0 >"$f" & p=$!; sleep 1; cansend can0 123##30102030405060708090A0B0C0D0E0F10; wait "$p" || test $? = 124; cat "$f"; grep -qi "123.*0102030405060708090A0B0C0D0E0F10" "$f"; rc=$?; ip link set can0 down; unlink "$f"; exit "$rc"'

run N2D-ROTATION 'mkdir -p "$work/n2d/bin"; ln -sfn /app/multimedia_samples/chip_base_test/10_gpu_2d_test/bin/rotation "$work/n2d/bin/rotation"; ln -sfn /app/multimedia_samples/sample_gpu_2d/resource "$work/n2d/resource"; cd "$work/n2d/bin"; ./rotation -m 1 -c 3840 -r 2160 -i 100 | tee "$work/n2d-rotation.log"; grep -q "Case rotation" "$work/n2d-rotation.log"'

run PREP-USER-SAMPLES 'cp /app/multimedia_samples/Makefile.in "$work/"; for d in sample_crypto sample_dsp sample_imu sample_osd sample_vse sysinfopro; do cp -a "/app/multimedia_samples/$d" "$work/"; done; cp -a /app/multimedia_samples/utils /app/multimedia_samples/vp_sensors "$work/"'
run BUILD-DSP 'make -C "$work/sample_dsp" clean all'
run BUILD-IMU 'make -C "$work/sample_imu" clean all'
run BUILD-OSD 'make -C "$work/sample_osd" clean all'
run BUILD-VSE 'make -C "$work/sample_vse" clean all'
run BUILD-SYSINFO 'make -C "$work/sysinfopro" clean all'

run BUILD-RSA 'make -C "$work/sample_crypto/sample_rsa" clean all'
run RSA-2048 'cd "$work/sample_crypto/sample_rsa"; timeout 90 ./sample_rsa -k 2048'
run RSA-3072 'cd "$work/sample_crypto/sample_rsa"; timeout 90 ./sample_rsa -k 3072'
run RSA-4096 'cd "$work/sample_crypto/sample_rsa"; timeout 120 ./sample_rsa -k 4096'
run TEE-INVENTORY 'ls -l /dev/tee*; find /usr /opt /app -name tee_client_api.h -o -name "*.ta" 2>/dev/null | sort | head -200; test -e /usr/include/tee_client_api.h'
run DSP-RUN 'cd "$work/sample_dsp"; timeout --signal=INT 30 ./sample_dsp -p /usr/hobot/lib/firmware -t 1 -n adsp'
run IMU-INVENTORY 'find /sys/bus/iio/devices -maxdepth 2 -type f -print 2>/dev/null | sort; ls -l /dev/iio* 2>/dev/null'
run VSE-FEEDBACK 'cd "$work/sample_vse"; printf "q\n" | timeout 30 script -qec "./sample_vse -i /app/multimedia_samples/sample_codec/1920x1080_NV12.yuv -w 1920 -h 1080 -f" /dev/null'
run OSD-COVER 'cd "$work/sample_osd"; printf "q\n" | timeout 30 script -qec "./sample_osd -i /app/multimedia_samples/sample_osd/1280x720_NV12.yuv -w 1280 -h 720 -m 1 -f" /dev/null'
run OSD-WORD 'cd "$work/sample_osd"; printf "q\n" | timeout 30 script -qec "./sample_osd -i /app/multimedia_samples/sample_osd/1280x720_NV12.yuv -w 1280 -h 720 -m 2 -f" /dev/null'
run OSD-LINE 'cd "$work/sample_osd"; printf "q\n" | timeout 30 script -qec "./sample_osd -i /app/multimedia_samples/sample_osd/1280x720_NV12.yuv -w 1280 -h 720 -m 3 -f" /dev/null'
run OSD-MOSAIC 'cd "$work/sample_osd"; printf "q\n" | timeout 30 script -qec "./sample_osd -i /app/multimedia_samples/sample_osd/1280x720_NV12.yuv -w 1280 -h 720 -m 4 -f" /dev/null'
run SYSINFO 'cd "$work/sysinfopro"; ./sysinfopro -h'

run FINAL-KERNEL 'dmesg | tail -n 800 | grep -Ei "BUG:|Oops:|panic|Call trace:|lockup|stall|SError" && exit 1 || exit 0'
run FINAL-PROCESSES 'for n in sample_dsp sample_imu sample_osd sample_vse glmark2 clpeak candump; do pgrep -x "$n" && exit 1 || true; done; exit 0'
printf '\nRETEST_SUMMARY pass=%d fail=%d log=%s\n' "$pass" "$fail" "$out"
