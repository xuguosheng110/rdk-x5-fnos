#!/usr/bin/env bash
set -euo pipefail

if [[ "${FNOS_WRITTEN_AUTHORIZATION:-}" != "YES" ]]; then
  echo "Refusing to inspect fnOS contents without documented written authorization." >&2
  echo "Set FNOS_WRITTEN_AUTHORIZATION=YES only after the authorization is recorded." >&2
  exit 3
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 IMAGE OUTPUT_DIR" >&2
  exit 2
fi

image=$1
output_dir=$2
loopdev=
mount_root=

cleanup() {
  if [[ -n "$mount_root" && -d "$mount_root" ]]; then
    while read -r mountpoint; do
      umount "$mountpoint" 2>/dev/null || true
    done < <(findmnt -rn -o TARGET -R "$mount_root" 2>/dev/null | sort -r)
    rmdir "$mount_root" 2>/dev/null || true
  fi
  if [[ -n "$loopdev" ]]; then
    losetup -d "$loopdev" 2>/dev/null || true
  fi
}
trap cleanup EXIT

mkdir -p "$output_dir"
fdisk -l "$image" >"${output_dir}/partition-table.txt"
loopdev=$(losetup --find --show --read-only --partscan "$image")
udevadm settle
lsblk -f "$loopdev" >"${output_dir}/filesystems.txt"

mount_root=$(mktemp -d /tmp/fnos-inspect.XXXXXX)
for part in "${loopdev}"p*; do
  [[ -b "$part" ]] || continue
  part_name=$(basename "$part")
  part_mount="${mount_root}/${part_name}"
  mkdir -p "$part_mount"
  if mount -o ro,noload "$part" "$part_mount" 2>/dev/null || mount -o ro "$part" "$part_mount" 2>/dev/null; then
    find "$part_mount" -maxdepth 3 -printf '%M %u:%g %s %p\n' \
      >"${output_dir}/${part_name}-tree.txt"
    if [[ -r "${part_mount}/etc/os-release" ]]; then
      cp "${part_mount}/etc/os-release" "${output_dir}/${part_name}-os-release.txt"
    fi
    if [[ -d "${part_mount}/etc/systemd/system" ]]; then
      find "${part_mount}/etc/systemd/system" -maxdepth 3 -type l -printf '%p -> %l\n' \
        >"${output_dir}/${part_name}-enabled-units.txt"
    fi
    if [[ -d "${part_mount}/usr/lib/systemd/system" ]]; then
      find "${part_mount}/usr/lib/systemd/system" -maxdepth 1 -type f -printf '%f\n' | sort \
        >"${output_dir}/${part_name}-available-units.txt"
    fi
    if [[ -d "${part_mount}/boot" ]]; then
      find "${part_mount}/boot" -maxdepth 4 -type f -printf '%p %s bytes\n' \
        >"${output_dir}/${part_name}-boot-files.txt"
    fi
    for path in usr/trim var/apps usr/local/bin; do
      if [[ -e "${part_mount}/${path}" ]]; then
        find "${part_mount}/${path}" -maxdepth 3 -printf '%M %u:%g %s /%P\n' \
          >"${output_dir}/${part_name}-$(tr / - <<<"$path").txt"
      fi
    done
    umount "$part_mount"
  fi
done

printf 'Read-only inspection complete: %s\n' "$output_dir"
