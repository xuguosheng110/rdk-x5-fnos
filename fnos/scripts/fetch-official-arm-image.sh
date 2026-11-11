#!/usr/bin/env bash
set -euo pipefail

version=1.2.0302
device=bcm27xx
build=2261
expected_md5=0ef3f3df011949101274ad5b4722a471
official_url="https://download.liveupdate.fnnas.com/arm/image/${version}/${device}/fnos_Mainland-PE_arm_${version}_${device}_${build}.img.gz"
thunder_url="http://thunder.liveupdate.fnnas.com:8080/arm/image/${version}/${device}/fnos_Mainland-PE_arm_${version}_${device}_${build}.img.gz"
output_dir=${1:-.}
output_file="${output_dir}/$(basename "$official_url")"

mkdir -p "$output_dir"

signed_url=$(
  curl -fsS 'https://fnnas.com/asset/download-sign' \
    -H 'content-type: application/json' \
    --data "{\"url\":\"${official_url}\"}" |
    jq -er '.url'
)

if ! curl --fail --location --continue-at - --retry 3 --retry-delay 2 \
  --output "$output_file" "$thunder_url"; then
  echo "Official Thunder endpoint failed; resuming from the signed HTTPS URL." >&2
  curl --fail --location --continue-at - --retry 5 --retry-delay 2 \
    --output "$output_file" "$signed_url"
fi

actual_md5=$(md5sum "$output_file" | awk '{print $1}')
if [[ "$actual_md5" != "$expected_md5" ]]; then
  echo "MD5 mismatch: expected ${expected_md5}, got ${actual_md5}" >&2
  exit 1
fi

sha256sum "$output_file" >"${output_file}.sha256"
printf 'Verified official fnOS ARM image: %s\n' "$output_file"
