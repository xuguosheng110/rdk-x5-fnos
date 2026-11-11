#!/usr/bin/env bash
# Collect a reproducible, non-destructive RDK X5 Linux 6.18 validation log.
# Run on the board as root. Optional write/stress tests are deliberately separate.
set -uo pipefail

OUT=${1:-rdk-x5-linux-6.18-validation.log}
if [[ ${VALIDATION_CONSOLE:-1} == 1 ]]; then
    exec > >(tee -a "$OUT") 2>&1
else
    exec >>"$OUT" 2>&1
fi

section() {
    printf '\n===== %s =====\n' "$1"
}

run() {
    printf '\n$ %s\n' "$*"
    "$@"
    local rc=$?
    printf '[exit=%d]\n' "$rc"
    return 0
}

shell_run() {
    printf '\n$ %s\n' "$1"
    bash -o pipefail -c "$1"
    local rc=$?
    printf '[exit=%d]\n' "$rc"
    return 0
}

section "TEST METADATA"
run date --iso-8601=seconds
run id
run hostnamectl
run uname -a
run cat /proc/version
run cat /etc/os-release
run sh -c 'command -v rdkos_info >/dev/null && rdkos_info || true'

section "BOOT AND COMMAND LINE"
run cat /proc/cmdline
run uptime
run cat /proc/uptime
run systemd-analyze
run systemd-analyze blame
run systemctl --failed --no-pager
run journalctl -b -p warning..alert --no-pager

section "DEVICE TREE"
run sh -c 'tr "\0" "\n" </sys/firmware/devicetree/base/compatible'
run sh -c 'tr "\0" "\n" </sys/firmware/devicetree/base/model'
run find /sys/firmware/devicetree/base/aliases -maxdepth 1 -type f -print

section "CPU AND MEMORY"
run lscpu
run nproc
run cat /proc/cpuinfo
run free -h
run cat /proc/meminfo
shell_run 'for c in /sys/devices/system/cpu/cpu[0-9]*; do printf "%s online=" "$c"; cat "$c/online" 2>/dev/null || echo 1; done'
shell_run 'for p in /sys/devices/system/cpu/cpufreq/policy*; do echo "[$p]"; grep -H . "$p"/{scaling_driver,scaling_governor,scaling_cur_freq,cpuinfo_min_freq,cpuinfo_max_freq} 2>/dev/null; done'

section "THERMAL"
shell_run 'for z in /sys/class/thermal/thermal_zone*; do printf "%s type=" "$z"; cat "$z/type" 2>/dev/null; printf "temp="; cat "$z/temp" 2>/dev/null; done'
shell_run 'find /sys/class/thermal -maxdepth 3 -type f \( -name trip_point_*_temp -o -name trip_point_*_type \) -print -exec cat {} \; 2>/dev/null'

section "STORAGE AND FILESYSTEM"
run lsblk -e7 -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,FSVER,LABEL,PARTLABEL,MOUNTPOINTS,MODEL
run findmnt
run df -hT
run cat /proc/partitions
run dmesg --level=err,warn

section "PCI USB AND NETWORK"
run sh -c 'command -v lspci >/dev/null && lspci -nnk || true'
run lsusb -t
run lsusb
run ip -details link show
run ip address show
run ip route show
run sh -c 'command -v ethtool >/dev/null && for n in /sys/class/net/*; do i=${n##*/}; [ "$i" = lo ] || ethtool "$i"; done || true'
run sh -c 'command -v iw >/dev/null && iw dev || true'
run rfkill list

section "GPIO PWM I2C SPI UART CAN"
run sh -c 'command -v gpioinfo >/dev/null && gpioinfo || true'
run find /sys/class/gpio -maxdepth 2 -type f -print
run find /sys/class/pwm -maxdepth 3 -print
run sh -c 'command -v i2cdetect >/dev/null && i2cdetect -l || true'
run find /dev -maxdepth 1 -type c -name 'i2c-*' -o -name 'spidev*' -o -name 'ttyS*' -o -name 'ttyAMA*'
run sh -c 'command -v ip >/dev/null && ip -details link show type can || true'

section "DISPLAY GPU AUDIO"
run find /dev/dri -maxdepth 1 -print
if [[ ${SKIP_DRM_PROBE:-0} == 1 ]]; then
    printf '\n$ modetest -c\n[skipped: DRM probe disabled for stability baseline]\n'
else
    run sh -c 'command -v modetest >/dev/null && modetest -c || true'
fi
run sh -c 'command -v glxinfo >/dev/null && glxinfo -B || true'
run sh -c 'command -v aplay >/dev/null && aplay -l || true'
run sh -c 'command -v arecord >/dev/null && arecord -l || true'
run cat /proc/asound/cards

section "CAMERA MEDIA AND CODEC"
run find /dev -maxdepth 1 \( -name 'video*' -o -name 'media*' -o -name 'v4l-*' \) -print
run sh -c 'command -v v4l2-ctl >/dev/null && v4l2-ctl --list-devices || true'
run sh -c 'command -v media-ctl >/dev/null && for m in /dev/media*; do [ -e "$m" ] && media-ctl -d "$m" -p; done || true'
run find /sys/class/video4linux -maxdepth 2 -type f -print

section "BPU"
run find /dev -maxdepth 2 \( -iname '*bpu*' -o -iname '*ion*' -o -iname '*hobot*' \) -print
shell_run 'find /sys -maxdepth 5 \( -iname "*bpu*" -o -iname "*hobot*" \) -print 2>/dev/null | head -300'
shell_run 'lsmod | grep -Ei "bpu|hobot|ion" || true'
shell_run 'dmesg | grep -Ei "bpu|hobot_dnn|hbmem|ion" || true'

section "KERNEL MODULES"
run cat /proc/sys/kernel/tainted
run lsmod
shell_run 'find /lib/modules/"$(uname -r)" -type f \( -name "*.ko" -o -name "*.ko.xz" -o -name "*.ko.zst" \) | sort'
shell_run 'find /lib/modules/"$(uname -r)" -type f \( -name "*.ko" -o -name "*.ko.xz" -o -name "*.ko.zst" \) | wc -l'
shell_run 'for m in $(awk "NR>1 {print \$1}" /proc/modules); do modinfo -F name "$m" 2>/dev/null; modinfo -F vermagic "$m" 2>/dev/null; done'
run depmod -n -a

section "INTERRUPTS CLOCKS AND REGULATORS"
run cat /proc/interrupts
run cat /proc/iomem
shell_run 'for s in /sys/kernel/debug/clk/clk_summary /sys/kernel/debug/regulator/regulator_summary; do [ -r "$s" ] && { echo "[$s]"; cat "$s"; }; done'

section "FINAL KERNEL DIAGNOSTICS"
shell_run 'dmesg | grep -Ei "BUG:|Oops:|WARNING:|Call trace:|panic|lockup|stall|timeout|failed|error|firmware" || true'
run cat /proc/sys/kernel/tainted
run uptime

printf '\nValidation collection complete: %s\n' "$OUT"
