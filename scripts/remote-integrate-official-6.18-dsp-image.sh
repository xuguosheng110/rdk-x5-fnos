#!/usr/bin/env bash
set -euo pipefail

source_image=/home/guosheng.xu/rdkx5-linux-6.18-work/base-images/rdk-x5-ubuntu22-preinstalled-desktop-3.5.0-arm64.img
kernel=/home/guosheng.xu/rdkx5-linux-6.18-work/x5-kernel-6.18
stage=/home/guosheng.xu/rdkx5-linux-6.18-work/staging-dsp
output_dir=/home/guosheng.xu/rdkx5-linux-6.18-work/images
output_image="$output_dir/rdk-x5-ubuntu22-desktop-3.5.0-linux-6.18.0-dsp.img"
log_dir=/home/guosheng.xu/rdkx5-linux-6.18-work/logs
release=6.18.0+

[[ $(id -u) -eq 0 ]] || {
    echo "run as root" >&2
    exit 1
}
test -f "$source_image"
test -f "$kernel/arch/arm64/boot/Image"
test -f "$kernel/arch/arm64/boot/dts/hobot/x5-rdk-v1p0.dtb"
test -d "$stage/lib/modules/$release"
test -f "$stage/lib/modules/$release/extra/bpu_hw_io_x5.ko"
mkdir -p "$output_dir" "$log_dir"
rm -f "$output_image" "$output_image.sha256"

mount_dir=$(mktemp -d /tmp/rdkx5-6.18-image.XXXXXX)
loop=
cleanup() {
    if mountpoint -q "$mount_dir"; then
        sync
        umount "$mount_dir"
    fi
    if [[ -n ${loop:-} ]]; then
        losetup -d "$loop" 2>/dev/null || true
    fi
    rmdir "$mount_dir" 2>/dev/null || true
}
trap cleanup EXIT

cp --reflink=auto --sparse=always "$source_image" "$output_image"
truncate -s +4G "$output_image"
parted --script "$output_image" resizepart 2 100%
loop=$(losetup --find --partscan --show "$output_image")
e2fsck -fy "${loop}p2"
resize2fs "${loop}p2"
e2fsck -fn "${loop}p2"
mount "${loop}p2" "$mount_dir"

test "$(. "$mount_dir/etc/os-release"; printf '%s' "$ID")" = ubuntu
test "$(. "$mount_dir/etc/os-release"; printf '%s' "$VERSION_ID")" = 22.04
test -x "$mount_dir/etc/init.d/S70loadko"
test -x "$mount_dir/usr/sbin/lightdm"

if [[ ! -f "$mount_dir/boot/Image.vendor-6.1.83" ]]; then
    install -m 0644 "$mount_dir/boot/Image" "$mount_dir/boot/Image.vendor-6.1.83"
fi
if [[ ! -f "$mount_dir/boot/hobot/x5-rdk-v1p0.vendor-6.1.83.dtb" ]]; then
    install -m 0644 "$mount_dir/boot/hobot/x5-rdk-v1p0.dtb" \
        "$mount_dir/boot/hobot/x5-rdk-v1p0.vendor-6.1.83.dtb"
fi

install -m 0644 "$kernel/arch/arm64/boot/Image" "$mount_dir/boot/Image"
install -m 0644 "$kernel/.config" "$mount_dir/boot/config-$release"
install -m 0644 "$kernel/System.map" "$mount_dir/boot/System.map-$release"
find "$kernel/arch/arm64/boot/dts/hobot" -maxdepth 1 -type f -name '*.dtb' \
    -exec install -m 0644 {} "$mount_dir/boot/hobot/" \;

mkdir -p "$mount_dir/lib/modules/$release"
rsync -aH --delete "$stage/lib/modules/$release/" \
    "$mount_dir/lib/modules/$release/"
rm -f "$mount_dir/lib/modules/$release/build" \
    "$mount_dir/lib/modules/$release/source"
depmod -ae -F "$kernel/System.map" -b "$mount_dir" "$release"

{
    echo "KERNEL_RELEASE=$release"
    echo "ROOTFS_ID=$(chroot "$mount_dir" /bin/sh -c '. /etc/os-release; printf "%s-%s" "$ID" "$VERSION_ID"')"
    echo "MODULE_COUNT=$(find "$mount_dir/lib/modules/$release" -type f -name '*.ko' | wc -l)"
    echo "DEPMOD=OK"
    sha256sum "$mount_dir/boot/Image" \
        "$mount_dir/boot/hobot/x5-rdk-v1p0.dtb" \
        "$mount_dir/lib/modules/$release/extra/bpu_hw_io_x5.ko"
} | tee "$log_dir/linux-6.18-dsp-image-content-validation-20260831.txt"

sync
umount "$mount_dir"
e2fsck -fn "${loop}p2"
losetup -d "$loop"
loop=
rmdir "$mount_dir"
trap - EXIT

sha256sum "$output_image" | tee "$output_image.sha256"
chown guosheng.xu:guosheng.xu "$output_image" "$output_image.sha256" \
    "$log_dir/linux-6.18-dsp-image-content-validation-20260831.txt"
echo "IMAGE=$output_image"
