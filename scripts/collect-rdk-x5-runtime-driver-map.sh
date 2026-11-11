#!/usr/bin/env bash
set -euo pipefail

output=${1:-rdk-x5-runtime-driver-map.tsv}
summary=${2:-rdk-x5-runtime-driver-map-summary.txt}
enabled_output=${3:-rdk-x5-enabled-dt-nodes.tsv}
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

printf 'sysfs_device\tdt_path\tstatus\tcompatible\tdriver\tmodule\tmodalias\tdriver_owner\n' >"$output"

find /sys/devices -type l -name of_node -print0 2>/dev/null |
while IFS= read -r -d '' link; do
    device=${link%/of_node}
    dt_real=$(readlink -f "$link")
    [[ $dt_real == /sys/firmware/devicetree/base* ]] || continue
    dt_path=${dt_real#/sys/firmware/devicetree/base}
    status=okay
    [[ -f "$dt_real/status" ]] && status=$(tr -d '\0' <"$dt_real/status")
    compatible=
    [[ -f "$dt_real/compatible" ]] && compatible=$(tr '\0' ';' <"$dt_real/compatible" | sed 's/;$//')
    driver=
    module=
    driver_owner=
    candidate=$device
    while [[ $candidate == /sys/devices/* ]]; do
        if [[ -L "$candidate/driver" ]]; then
            driver_owner=$candidate
            driver=$(basename "$(readlink -f "$candidate/driver")")
            break
        fi
        candidate=${candidate%/*}
    done
    if [[ -n $driver ]]; then
        if [[ -L "$driver_owner/driver/module" ]]; then
            module=$(basename "$(readlink -f "$driver_owner/driver/module")")
        else
            module=builtin
        fi
    fi
    modalias=
    [[ -r "$device/modalias" ]] && modalias=$(tr -d '\r\n' <"$device/modalias")
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$device" "${dt_path:-/}" "$status" "$compatible" "$driver" "$module" "$modalias" "$driver_owner"
done | sort -t $'\t' -k2,2 >>"$output"

printf 'dt_path\tstatus\tcompatible\n' >"$tmp_dir/enabled.tsv"
find /sys/firmware/devicetree/base -type f -name compatible -print0 2>/dev/null |
while IFS= read -r -d '' compatible_file; do
    node=${compatible_file%/compatible}
    status=okay
    [[ -f "$node/status" ]] && status=$(tr -d '\0' <"$node/status")
    [[ $status == okay || $status == ok ]] || continue
    dt_path=${node#/sys/firmware/devicetree/base}
    compatible=$(tr '\0' ';' <"$compatible_file" | sed 's/;$//')
    printf '%s\t%s\t%s\n' "${dt_path:-/}" "$status" "$compatible"
done | sort -t $'\t' -k1,1 >>"$tmp_dir/enabled.tsv"

awk -F '\t' '
    function append_unique(list, value) {
        if (value == "" || (";" list ";") ~ (";" value ";")) return list
        return (list ? list ";" : "") value
    }
    BEGIN {OFS="\t"}
    NR == FNR {
        if (FNR == 1) next
        path=$2
        mapped[path]=1
        devices[path]=(devices[path] ? devices[path] ";" : "") $1
        driver_list[path]=append_unique(driver_list[path], $5)
        module_list[path]=append_unique(module_list[path], $6)
        next
    }
    FNR == 1 {
        print $0, "runtime_mapped", "sysfs_devices", "drivers", "modules"
        next
    }
    {
        print $0, (mapped[$1] ? "yes" : "no"), devices[$1], driver_list[$1], module_list[$1]
    }
' "$output" "$tmp_dir/enabled.tsv" >"$enabled_output"

total=$(( $(wc -l <"$output") - 1 ))
bound=$(awk -F '\t' 'NR > 1 && $5 != "" {n++} END {print n+0}' "$output")
unbound=$(awk -F '\t' 'NR > 1 && $5 == "" {n++} END {print n+0}' "$output")
disabled=$(awk -F '\t' 'NR > 1 && $3 == "disabled" {n++} END {print n+0}' "$output")
enabled_total=$(( $(wc -l <"$enabled_output") - 1 ))
enabled_mapped=$(awk -F '\t' 'NR > 1 && $4 == "yes" {n++} END {print n+0}' "$enabled_output")
enabled_unmapped=$(awk -F '\t' 'NR > 1 && $4 == "no" {n++} END {print n+0}' "$enabled_output")
deferred=$(dmesg | grep -Eic 'deferred probe|probe deferral' || true)
firmware_errors=$(dmesg | grep -Eic 'firmware.*(failed|error)|failed to load.*firmware' || true)
unknown_symbols=$(dmesg | grep -Eic 'unknown symbol' || true)

{
    printf 'kernel=%s\n' "$(uname -r)"
    printf 'mapped_of_devices=%s\n' "$total"
    printf 'bound_devices=%s\n' "$bound"
    printf 'unbound_devices=%s\n' "$unbound"
    printf 'disabled_mapped_devices=%s\n' "$disabled"
    printf 'enabled_compatible_dt_nodes=%s\n' "$enabled_total"
    printf 'enabled_dt_nodes_runtime_mapped=%s\n' "$enabled_mapped"
    printf 'enabled_dt_nodes_without_runtime_device=%s\n' "$enabled_unmapped"
    printf 'deferred_probe_messages=%s\n' "$deferred"
    printf 'firmware_error_messages=%s\n' "$firmware_errors"
    printf 'unknown_symbol_messages=%s\n' "$unknown_symbols"
} | tee "$summary"
