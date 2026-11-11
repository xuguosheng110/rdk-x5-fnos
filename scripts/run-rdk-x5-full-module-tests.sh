#!/usr/bin/env bash
# Execute non-destructive RDK X5 module tests on the board and retain evidence.
# Kernel/image builds are intentionally excluded: those must run on the remote Linux builder.
set -uo pipefail

out=${1:-/tmp/rdk-x5-full-module-tests.log}
work=/tmp/rdk-x5-full-module-tests
export work
start_dmesg_lines=$(dmesg | wc -l)
pass=0
fail=0

exec > >(tee -a "$out") 2>&1

case_run() {
    local id=$1
    shift
    printf '\n===== %s =====\n$' "$id"
    printf ' %q' "$@"
    printf '\n'
    "$@"
    local rc=$?
    printf '[CASE %s RC=%d]\n' "$id" "$rc"
    if (( rc == 0 )); then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
    fi
    return 0
}

shell_case() {
    local id=$1
    local command=$2
    printf '\n===== %s =====\n$ %s\n' "$id" "$command"
    bash -e -o pipefail -c "$command"
    local rc=$?
    printf '[CASE %s RC=%d]\n' "$id" "$rc"
    if (( rc == 0 )); then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
    fi
    return 0
}

mkdir -p "$work"

case_run BASE-01 date --iso-8601=seconds
case_run BASE-02 uname -a
case_run BASE-03 systemctl --failed --no-pager
case_run BASE-04 cat /proc/sys/kernel/tainted
shell_case BASE-05 'systemctl is-active x5-loadko.service cam-service lightdm; uptime; free -h; df -hT /'
shell_case BOOT-01 'tr "\0" "\n" </sys/firmware/devicetree/base/model; tr "\0" "\n" </sys/firmware/devicetree/base/compatible; cat /proc/cmdline'
shell_case MODULE-01 'find "/lib/modules/$(uname -r)" -type f -name "*.ko*" | wc -l; depmod -n -a 2>&1 | grep -E "needs unknown symbol|ERROR" && exit 1 || exit 0'

shell_case CPU-01 'cd /app/multimedia_samples/chip_base_test/08_cpu_performance/coremark-main && timeout 120 ./coremark_O3_single'
shell_case CPU-02 'cd /app/multimedia_samples/chip_base_test/08_cpu_performance/coremark-main && timeout 120 ./coremark_O3_multi'
shell_case DDR-01 'cd /app/multimedia_samples/chip_base_test/09_ddr_bandwidth && timeout 120 ./stream'
shell_case THERMAL-01 'for z in /sys/class/thermal/thermal_zone*; do printf "%s " "$(basename "$z")"; cat "$z/type" "$z/temp" 2>/dev/null | tr "\n" " "; echo; done'

shell_case SD-01 'f=$(mktemp /tmp/rdkx5-sd-write.XXXXXX); dd if=/dev/urandom of="$f" bs=1M count=16 conv=fsync status=none; a=$(sha256sum "$f" | cut -d" " -f1); sync; b=$(sha256sum "$f" | cut -d" " -f1); unlink "$f"; test "$a" = "$b"; echo "sha256=$a"'
shell_case STORAGE-02 'lsblk -e7 -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS,MODEL; findmnt; cat /proc/partitions'

shell_case NET-01 'ip -br link; ip -br address; ip route; iw dev 2>/dev/null || true'
shell_case NET-02 'gw=$(ip route show default | awk "NR==1{print \$3}"); test -n "$gw"; ping -c 4 -W 2 "$gw"; ping -c 4 -W 2 1.1.1.1; getent ahostsv4 developer.d-robotics.cc | head'
shell_case NET-03 'for n in eth0 wlan0; do echo "[$n]"; ethtool "$n" 2>/dev/null || true; done'
shell_case BT-01 'rfkill list bluetooth; hciconfig -a; bluetoothctl show'
shell_case BT-02 'bluetoothctl power on; timeout 15 bluetoothctl scan on || test $? = 124; bluetoothctl devices; bluetoothctl scan off || true'

shell_case USB-01 'lsusb; lsusb -t; ls -l /sys/class/udc; find /sys/kernel/config/usb_gadget -maxdepth 3 -type f -print 2>/dev/null | sort | head -100'
shell_case GPIO-01 'gpioinfo 2>/dev/null || true; ls -l /dev/gpiochip*; find /sys/class/gpio -maxdepth 2 -print'
shell_case PWM-01 'ls -l /sys/class/pwm; find /sys/class/pwm -maxdepth 3 -type f -print -exec cat {} \; 2>/dev/null'
shell_case I2C-01 'i2cdetect -l; i2cdetect -y -r 4'
shell_case SPI-01 'ls -l /dev/spidev* 2>/dev/null || true; grep -E "spi|tcan" /proc/interrupts || true'
shell_case UART-01 'ls -l /dev/ttyS*; stty -F /dev/ttyS2 -a 2>/dev/null || true'
shell_case RTC-01 'ls -l /dev/rtc*; cat /proc/driver/rtc; hwclock --show --utc'
shell_case WATCHDOG-01 'ls -l /dev/watchdog*; for w in /sys/class/watchdog/watchdog*; do echo "[$w]"; grep -H . "$w"/{identity,status,timeout,timeleft,nowayout} 2>/dev/null || true; done'
shell_case CAN-01 'ip link set can0 down; ip link set can0 type can bitrate 500000 loopback on; ip link set can0 up; capture=$(mktemp); timeout 4 candump -L can0 >"$capture" & p=$!; sleep 1; cansend can0 618#061800A55A123456; wait "$p" || test $? = 124; cat "$capture"; grep -qi "618.*061800A55A123456" "$capture"; rc=$?; ip link set can0 down; unlink "$capture"; exit "$rc"'

shell_case AUDIO-01 'aplay -l; arecord -l; cat /proc/asound/cards'
shell_case AUDIO-02 'timeout 8 speaker-test -D hw:0,0 -t sine -f 1000 -c 2 -l 1'
shell_case AUDIO-03 'f=$(mktemp --suffix=.wav); timeout 8 arecord -D hw:0,0 -f S16_LE -r 48000 -c 2 -d 2 "$f"; stat -c "%n %s" "$f"; test "$(stat -c %s "$f")" -gt 100000; unlink "$f"'

shell_case N2D-01 'cd /app/multimedia_samples/chip_base_test/10_gpu_2d_test && timeout 180 ./performance_test.sh'
shell_case GPU3D-01 'cd /app/multimedia_samples/chip_base_test/11_gpu_3d_test/clpeak && timeout 180 ./clpeak'
shell_case GPU3D-02 'cd /app/multimedia_samples/chip_base_test/11_gpu_3d_test/glmark2 && timeout 90 ./bin/glmark2 --off-screen --validate --data-path ./data'

shell_case SAMPLE-PREP-01 'cp /app/multimedia_samples/Makefile.in "$work/"; for d in sample_crypto sample_hbmem sample_trustzone/sample_ca sample_dsp sample_imu sample_osd sample_vse sysinfopro; do mkdir -p "$work/$(dirname "$d")"; cp -a "/app/multimedia_samples/$d" "$work/$d"; done; cp -a /app/multimedia_samples/utils /app/multimedia_samples/vp_sensors "$work/"'
shell_case BUILD-CRYPTO-01 'for d in sample_crypto/sample_cipher sample_crypto/sample_digest sample_crypto/sample_hmac sample_crypto/sample_rsa; do make -C "$work/$d" clean all; done'
shell_case BUILD-HBMEM-01 'make -C "$work/sample_hbmem" clean all'
shell_case BUILD-TEE-01 'make -C "$work/sample_trustzone/sample_ca" clean all'
shell_case BUILD-DSP-01 'make -C "$work/sample_dsp" clean all'
shell_case BUILD-IMU-01 'make -C "$work/sample_imu" clean all'
shell_case BUILD-OSD-01 'make -C "$work/sample_osd" clean all'
shell_case BUILD-VSE-01 'make -C "$work/sample_vse" clean all'
shell_case BUILD-SYSINFO-01 'make -C "$work/sysinfopro" clean all'
shell_case CRYPTO-01 'cd "$work/sample_crypto/sample_cipher"; for spec in aes_ecb:128 aes_cbc:256 aes_cfb:192 aes_ofb:128 aes_ctr:256 sm4_ecb:128 sm4_cbc:128 sm4_cfb:128 sm4_ofb:128 sm4_ctr:128 des_ecb:64 des_cbc:64 des_ofb:64; do ./sample_cipher -s "${spec%:*}" -k "${spec#*:}" || exit; done'
shell_case CRYPTO-02 'cd "$work/sample_crypto/sample_digest"; for a in md5 sha1 sha224 sha256 sha384 sha512 sha3-224 sha3-256 sha3-384 sha3-512; do ./sample_digest -s "$a" || exit; done'
shell_case CRYPTO-03 'cd "$work/sample_crypto/sample_hmac"; for spec in md5:128 sha1:160 sha224:224 sha256:256 sha384:384 sha512:512 sha3-224:224 sha3-256:256 sha3-384:384 sha3-512:512; do ./sample_hmac -s "${spec%:*}" -k "${spec#*:}" || exit; done'
shell_case CRYPTO-04 'cd "$work/sample_crypto/sample_rsa"; for bits in 1024 2048 3072 4096; do timeout 60 ./sample_rsa -k "$bits" || exit; done'
shell_case HBMEM-01 'cd "$work/sample_hbmem"; for mode in $(seq 1 12); do timeout 30 ./sample_hbmem -m "$mode" || exit; done'
shell_case TEE-01 'test -e /dev/tee0; cd "$work/sample_trustzone/sample_ca"; ./sample_ca -i 1; ./sample_ca -d 1; ./sample_ca -c rdk-x5-6.18'
shell_case DSP-01 'ls -l /dev/dsp* /sys/class/remoteproc 2>/dev/null || true; cd "$work/sample_dsp"; timeout --signal=INT 30 ./sample_dsp -p /usr/hobot/lib/firmware -t 1 -n adsp'
shell_case IMU-01 'ls -l /dev/iio* /sys/bus/iio/devices 2>/dev/null || true; cd "$work/sample_imu"; timeout 15 script -qec "./sample_imu -n bmi08x" /dev/null'
shell_case VSE-01 'cd "$work/sample_vse"; timeout 30 script -qec "./sample_vse -i /app/multimedia_samples/sample_codec/1920x1080_NV12.yuv -w 1920 -h 1080 -f" /dev/null'
shell_case OSD-01 'cd "$work/sample_osd"; for mode in 1 2 3 4; do timeout 30 script -qec "./sample_osd -i /app/multimedia_samples/sample_osd/1280x720_NV12.yuv -w 1280 -h 720 -m $mode -f" /dev/null || exit; done'
shell_case SYSINFO-01 'cd "$work/sysinfopro"; ./sysinfopro -h; rdkos_info; hrut_somstatus'

shell_case CAMERA-01 'systemctl is-active cam-service; i2cdetect -y -r 4; test -e /tmp/mipi_sensor_detect && /tmp/mipi_sensor_detect || true'
shell_case CODEC-01 'ls -l /dev/hobot-vpu /dev/hobot-jpu 2>/dev/null || true; lsmod | grep -E "vpu|jpu|codec" || true'
shell_case DISPLAY-01 'for s in /sys/class/drm/card*-*/status; do echo -n "$s "; cat "$s"; done; for e in /sys/class/drm/card*-*/edid; do echo -n "$e "; wc -c <"$e"; done'

new_dmesg="$work/dmesg-new.log"
dmesg | tail -n +$((start_dmesg_lines + 1)) > "$new_dmesg"
shell_case FINAL-01 'grep -Ei "BUG:|Oops:|panic|Call trace:|lockup|stall|SError" "$work/dmesg-new.log" && exit 1 || exit 0'
shell_case FINAL-02 'systemctl --failed --no-pager || true; for n in sample_cipher sample_digest sample_hmac sample_rsa sample_hbmem sample_ca sample_dsp sample_imu sample_osd sample_vse glmark2 clpeak coremark_O3_single coremark_O3_multi stream candump; do pgrep -x "$n" && exit 1 || true; done; exit 0'
printf '\nFULL_MODULE_SUMMARY pass=%d fail=%d log=%s\n' "$pass" "$fail" "$out"
