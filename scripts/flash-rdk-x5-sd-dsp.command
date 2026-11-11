#!/bin/bash
set -euo pipefail

image=/Volumes/data/rdk-x5-linux-6.18-official/rdk-x5-ubuntu22-desktop-3.5.0-linux-6.18.0-dsp.img
expected_target_size=31914983424
expected_image_size=14688452608
expected_sha=2320bbacf0df4a58425faf0f20cf709f0d95f04337257547c89beab78852ec2b
image_mib=14008
status_file="/tmp/rdk-x5-flash-dsp-${UID}.status"

test -f "$image"
actual_size=$(stat -f %z "$image")
[[ $actual_size == "$expected_image_size" ]]
actual_sha=$(shasum -a 256 "$image" | awk '{print $1}')
[[ $actual_sha == "$expected_sha" ]]

targets=()
for candidate in /dev/disk[0-9]*; do
    [[ $candidate =~ ^/dev/disk[0-9]+$ ]] || continue
    info=$(diskutil info "$candidate" 2>/dev/null || true)
    if printf '%s\n' "$info" | grep -q 'Device / Media Name:.*UMS disk 0' &&
       printf '%s\n' "$info" | grep -q 'Protocol:.*USB' &&
       printf '%s\n' "$info" | grep -q "Disk Size:.*(${expected_target_size} Bytes)"; then
        targets+=("$candidate")
    fi
done

if (( ${#targets[@]} != 1 )); then
    printf 'Expected exactly one verified RDK X5 SD UMS target, found %d\n' "${#targets[@]}" >&2
    exit 3
fi

target=${targets[0]}
raw_target=${target/\/dev\/disk/\/dev\/rdisk}
printf 'STARTED target=%s image=%s\n' "$target" "$image" > "$status_file"
echo "Target verified: $target, UMS disk 0, ${expected_target_size} bytes"
echo "Writing ${expected_image_size} bytes and reading back ${image_mib} MiB..."
sudo -v

while read -r mounted_device; do
    [[ -n $mounted_device ]] || continue
    diskutil unmount "$mounted_device"
done < <(mount | awk -v prefix="${target}s" '$1 ~ ("^" prefix "[0-9]+$") {print $1}')

final_info=$(diskutil info "$target")
printf '%s\n' "$final_info" | grep -q 'Device / Media Name:.*UMS disk 0'
printf '%s\n' "$final_info" | grep -q 'Protocol:.*USB'
printf '%s\n' "$final_info" | grep -q "Disk Size:.*(${expected_target_size} Bytes)"
[[ -c $raw_target ]]

sudo dd if="$image" of="$raw_target" bs=8388608
sync
readback_sha=$(sudo dd if="$raw_target" bs=1048576 count="$image_mib" | shasum -a 256 | awk '{print $1}')

if [[ $readback_sha != "$expected_sha" ]]; then
    printf 'VERIFY_FAILED expected=%s actual=%s\n' "$expected_sha" "$readback_sha" | tee "$status_file"
    exit 1
fi

printf 'PASS target=%s sha256=%s\n' "$target" "$readback_sha" | tee "$status_file"
echo 'FLASH AND READ-BACK VERIFY COMPLETE.'
