#!/usr/bin/env bash
set -euo pipefail

if (( $# != 3 )); then
    echo "usage: $0 OFFICIAL.img.xz BOARD_BACKUP_DIR OUTPUT.img" >&2
    exit 2
fi

source_xz=$(readlink -f "$1")
backup=$(readlink -f "$2")
output=$(readlink -m "$3")

test -f "$source_xz"
test -f "$backup/boot/Image"
test -f "$backup/boot/hobot/x5-rdk-v1p0.dtb"
test -d "$backup/lib/modules/6.18.0"
[[ $output == /Volumes/data/rdk-x5-linux-6.18-official/* ]]
[[ ! -e $output ]]

work=$(mktemp -d /tmp/rdkx5-official-6.18.XXXXXX)
loop=
root=
cleanup() {
    if [[ -n $root ]] && mountpoint -q "$root"; then
        sync
        umount "$root"
    fi
    if [[ -n $loop ]]; then
        losetup -d "$loop" 2>/dev/null || true
    fi
    rmdir "$work/root" 2>/dev/null || true
    rmdir "$work" 2>/dev/null || true
}
trap cleanup EXIT

echo "Decompressing official image to $output"
xz -dc -- "$source_xz" > "$output"
sync

loop=$(losetup --find --partscan --show "$output")
root="$work/root"
mkdir "$root"

e2fsck -fn "${loop}p2"
mount "${loop}p2" "$root"

test "$(. "$root/etc/os-release"; printf '%s' "$ID")" = ubuntu
test "$(. "$root/etc/os-release"; printf '%s' "$VERSION_ID")" = 22.04
test -x "$root/etc/init.d/S70loadko"

install -m 0644 "$root/boot/Image" "$root/boot/Image.vendor-6.1.83"
install -m 0644 "$root/boot/hobot/x5-rdk-v1p0.dtb" \
    "$root/boot/hobot/x5-rdk-v1p0.vendor-6.1.83.dtb"

install -m 0644 "$backup/boot/Image" "$root/boot/Image"
install -m 0644 "$backup/boot/config-6.18.0" "$root/boot/config-6.18.0"
install -m 0644 "$backup/boot/System.map-6.18.0" "$root/boot/System.map-6.18.0"
install -m 0644 "$backup/boot/hobot/x5-rdk-v1p0.dtb" \
    "$root/boot/hobot/x5-rdk-v1p0.dtb"

mkdir -p "$root/lib/modules/6.18.0"
cp -a "$backup/lib/modules/6.18.0/." "$root/lib/modules/6.18.0/"

install -m 0644 "$backup/etc/systemd/system/x5-loadko.service" \
    "$root/etc/systemd/system/x5-loadko.service"
mkdir -p "$root/etc/systemd/system/multi-user.target.wants"
ln -sfn ../x5-loadko.service \
    "$root/etc/systemd/system/multi-user.target.wants/x5-loadko.service"

sync
sha256sum \
    "$root/boot/Image" \
    "$root/boot/hobot/x5-rdk-v1p0.dtb" \
    "$root/lib/modules/6.18.0/updates/bpu_hw_io_x5.ko"
grep -q '^kernel/' "$root/lib/modules/6.18.0/modules.dep"
test -L "$root/etc/systemd/system/multi-user.target.wants/x5-loadko.service"

umount "$root"
root=
e2fsck -fn "${loop}p2"
losetup -d "$loop"
loop=

sha256sum "$output" > "$output.sha256"
echo "Created $output"
