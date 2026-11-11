#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 2
fi

if ! systemctl is-active --quiet NetworkManager; then
  echo "ERROR: NetworkManager is not active; refusing to disable ifupdown" >&2
  exit 3
fi

if [[ -s /etc/network/interfaces ]]; then
  echo "ERROR: /etc/network/interfaces contains configuration; inspect manually" >&2
  exit 4
fi

effective_dnsmasq_config=$(
  grep -RhsEv '^[[:space:]]*(#|$)' /etc/dnsmasq.conf /etc/dnsmasq.d 2>/dev/null || true
)
if [[ -n ${effective_dnsmasq_config} ]]; then
  echo "ERROR: dnsmasq has active configuration; inspect manually" >&2
  exit 5
fi

# The image uses NetworkManager/netplan.  The enabled ifupdown unit cannot
# start because /etc/network/interfaces is absent, so it is an obsolete path.
systemctl disable --now networking.service

# No DNS/DHCP options are configured for dnsmasq.  Its wildcard DNS listener
# only conflicts with systemd-resolved on port 53.
systemctl disable --now dnsmasq.service

install -d -m 0755 /etc/systemd/system/auto_update_miniboot.service.d
install -m 0644 /tmp/rdkx5-exit-status.conf \
  /etc/systemd/system/auto_update_miniboot.service.d/exit-status.conf

systemctl daemon-reload
systemctl reset-failed networking.service dnsmasq.service auto_update_miniboot.service
systemctl start auto_update_miniboot.service
systemctl start apt-show-versions.service
systemctl reset-failed

echo "system_state=$(systemctl is-system-running || true)"
systemctl --failed --no-legend || true
echo "network_manager=$(systemctl is-active NetworkManager)"
echo "wifi_ipv4=$(ip -4 -brief address show wlan0 | awk '{print $3}')"
