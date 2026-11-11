#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 KERNEL_SOURCE BASELINE_CONFIG NEW_CONFIG [ALLOWLIST]" >&2
  exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source "$repo_root/kernel-6.18.env"

kernel_source=$1
baseline=$2
candidate=$3
allowlist=${4:-$repo_root/kernel-6.18-config-allowlist.txt}
work=$(mktemp -d "${TMPDIR:-/tmp}/rdkx5-kernel-config.XXXXXX")
trap 'rm -rf "$work"' EXIT

read_config() {
  local input=$1 output=$2
  if [[ $input == *.gz ]]; then
    gzip -cd -- "$input" >"$output"
  else
    cp -- "$input" "$output"
  fi
}

read_config "$baseline" "$work/baseline"
read_config "$candidate" "$work/candidate"

makefile=${kernel_source%/}/Makefile
test -f "$makefile" || { echo "missing kernel Makefile: $makefile" >&2; exit 2; }
version=$(awk '$1 == "VERSION" && $2 == "=" {print $3}' "$makefile")
patchlevel=$(awk '$1 == "PATCHLEVEL" && $2 == "=" {print $3}' "$makefile")
[[ "$version.$patchlevel" == "$RDK_KERNEL_SERIES" ]] || {
  echo "candidate source is $version.$patchlevel; expected $RDK_KERNEL_SERIES" >&2
  exit 1
}

awk -F= '/^CONFIG_[A-Za-z0-9_]+=[ym]$/ {print $1}' "$work/baseline" | sort -u >"$work/baseline.enabled"
awk -F= '/^CONFIG_[A-Za-z0-9_]+=[ym]$/ {print $1}' "$work/candidate" | sort -u >"$work/candidate.enabled"
comm -23 "$work/baseline.enabled" "$work/candidate.enabled" >"$work/missing.raw"

if [[ -s $allowlist ]]; then
  sed -E '/^[[:space:]]*(#|$)/d' "$allowlist" >"$work/allowlist"
  if [[ -s "$work/allowlist" ]]; then
    grep -Ev -f "$work/allowlist" "$work/missing.raw" >"$work/missing" || true
  else
    cp "$work/missing.raw" "$work/missing"
  fi
else
  cp "$work/missing.raw" "$work/missing"
fi

if [[ -s "$work/missing" ]]; then
  echo "6.18 candidate silently drops enabled 6.1.83 capabilities:" >&2
  sed -n '1,200p' "$work/missing" >&2
  count=$(wc -l <"$work/missing" | tr -d ' ')
  echo "$count missing CONFIG entries; port them or document each rename/removal in $allowlist" >&2
  exit 1
fi

echo "kernel target: $RDK_KERNEL_SERIES"
echo "baseline enabled configs: $(wc -l <"$work/baseline.enabled" | tr -d ' ')"
echo "candidate enabled configs: $(wc -l <"$work/candidate.enabled" | tr -d ' ')"
echo "config compatibility gate passed"
