#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 ]]; then
    echo "usage: $0 KERNEL_SOURCE STAGING_ROOT OUTPUT_DIR" >&2
    exit 2
fi

kernel_source=$(realpath "$1")
staging_root=$(realpath "$2")
output_dir=$3
release=$(make -s -C "$kernel_source" ARCH=arm64 kernelrelease)
module_root="$staging_root/lib/modules/$release"

mkdir -p "$output_dir"
csv="$output_dir/modules.csv"
summary="$output_dir/summary.txt"
depmod_log="$output_dir/depmod.log"

[[ -f "$kernel_source/modules.order" ]] || {
    echo "missing modules.order in $kernel_source" >&2
    exit 1
}
[[ -d "$module_root" ]] || {
    echo "missing installed module tree $module_root" >&2
    exit 1
}

printf 'index,build_path,installed_path,present,name,vermagic,depends,sha256\n' >"$csv"
index=0
missing=0
bad_vermagic=0

while IFS= read -r build_path; do
    index=$((index + 1))
    installed_rel=${build_path%.o}.ko
    installed_path="$module_root/kernel/$installed_rel"
    if [[ ! -f "$installed_path" ]]; then
        printf '%d,"%s","%s",no,,,,\n' "$index" "$build_path" "$installed_path" >>"$csv"
        missing=$((missing + 1))
        continue
    fi

    name=$(modinfo -F name "$installed_path" | tr -d '\r\n')
    vermagic=$(modinfo -F vermagic "$installed_path" | tr -d '\r\n')
    depends=$(modinfo -F depends "$installed_path" | tr -d '\r\n')
    sha=$(sha256sum "$installed_path" | cut -d' ' -f1)
    [[ $vermagic == "$release "* ]] || bad_vermagic=$((bad_vermagic + 1))
    printf '%d,"%s","%s",yes,"%s","%s","%s",%s\n' \
        "$index" "$build_path" "$installed_path" "$name" "$vermagic" "$depends" "$sha" >>"$csv"
done <"$kernel_source/modules.order"

set +e
depmod -e -F "$kernel_source/System.map" -b "$staging_root" "$release" >"$depmod_log" 2>&1
depmod_rc=$?
set -e
unknown_symbols=$(grep -Ec 'needs unknown symbol|Unknown symbol' "$depmod_log" || true)
installed_count=$(find "$module_root" -type f -name '*.ko' | wc -l | tr -d ' ')
order_count=$(wc -l <"$kernel_source/modules.order" | tr -d ' ')

{
    printf 'kernel_source=%s\n' "$kernel_source"
    printf 'kernel_commit=%s\n' "$(git -C "$kernel_source" rev-parse HEAD)"
    printf 'kernel_release=%s\n' "$release"
    printf 'modules_order=%s\n' "$order_count"
    printf 'installed_modules=%s\n' "$installed_count"
    printf 'missing_modules=%s\n' "$missing"
    printf 'bad_vermagic=%s\n' "$bad_vermagic"
    printf 'depmod_rc=%s\n' "$depmod_rc"
    printf 'unknown_symbols=%s\n' "$unknown_symbols"
} | tee "$summary"

[[ $missing == 0 && $bad_vermagic == 0 && $depmod_rc == 0 && $unknown_symbols == 0 ]]
