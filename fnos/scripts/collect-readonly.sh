#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: SSHPASS=... $0 USER HOST" >&2
  exit 2
fi

remote_user=$1
remote_host=$2

if [[ -z "${SSHPASS:-}" ]]; then
  echo "SSHPASS must be supplied through the environment (never stored in this repository)." >&2
  exit 2
fi

sshpass -e ssh \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR \
  "${remote_user}@${remote_host}" 'bash -s' <<'REMOTE'
set -u

section() { printf '\n===== %s =====\n' "$1"; }
run() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1 || printf '[exit=%s]\n' "$?"
}

section identity
run date -u +%FT%TZ
run id
run uname -a
run cat /etc/os-release
run hostnamectl

section cpu-memory
run lscpu
run free -h

section board-device-tree
if [[ -r /proc/device-tree/model ]]; then
  printf 'model='; tr -d '\0' </proc/device-tree/model; printf '\n'
fi
if [[ -r /proc/device-tree/compatible ]]; then
  printf 'compatible='; tr '\0' ',' </proc/device-tree/compatible; printf '\n'
fi
run find /proc/device-tree -maxdepth 2 -type f -name status -print

section boot-kernel
run cat /proc/cmdline
run cat /proc/version
run ls -la /boot
run find /boot -maxdepth 3 -type f -printf '%p %s bytes\n'
run ls -la /lib/modules
if [[ -r /proc/config.gz ]]; then
  printf '\n$ zcat /proc/config.gz | selected CONFIG entries\n'
  zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_(ARM64|ARCH_HOBOT|EFI|OF|OVERLAY_FS|SQUASHFS|BTRFS_FS|EXT4_FS|NAMESPACES|CGROUPS|CGROUP_BPF|SECCOMP|DOCKER|BRIDGE|VETH|BLK_DEV_DM|MD|SATA|USB_STORAGE)=' || true
elif [[ -r "/boot/config-$(uname -r)" ]]; then
  printf '\n$ grep selected CONFIG entries /boot/config-%s\n' "$(uname -r)"
  grep -E '^CONFIG_(ARM64|ARCH_HOBOT|EFI|OF|OVERLAY_FS|SQUASHFS|BTRFS_FS|EXT4_FS|NAMESPACES|CGROUPS|CGROUP_BPF|SECCOMP|DOCKER|BRIDGE|VETH|BLK_DEV_DM|MD|SATA|USB_STORAGE)=' "/boot/config-$(uname -r)" || true
fi
run sh -c 'command -v fw_printenv || true'
run sh -c 'command -v extlinux || true'
run sh -c 'find /boot /etc -maxdepth 3 \( -name "extlinux.conf" -o -name "uEnv.txt" -o -name "grub.cfg" \) -type f -print 2>/dev/null'

section storage
run lsblk -e7 -o NAME,PATH,TYPE,SIZE,FSTYPE,FSVER,LABEL,PARTLABEL,PARTUUID,UUID,MOUNTPOINTS,MODEL,TRAN
run findmnt -R /
run df -hT
run df -ih
run cat /proc/partitions

section network
run ip -brief address
run ip route
run resolvectl status
run ss -lntup

section containers
run sh -c 'command -v docker || true'
run docker version
run docker info
run sh -c 'command -v podman || true'
run sh -c 'command -v containerd || true'
run systemctl is-active docker
run systemctl is-enabled docker

section hardware
run lspci -nn
run lsusb
run sh -c 'find /dev -maxdepth 2 \( -name "video*" -o -name "dri" -o -name "npu*" -o -name "ion" \) -print'
run sh -c 'dmesg | grep -Ei "hobot|x5|mmc|nvme|sata|ethernet|npu|iommu" | tail -n 160'
REMOTE
