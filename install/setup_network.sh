#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# setup_network.sh — Configure the 10G NIC for FLIR GigE cameras:
#   - Static IP (192.168.1.1/24) via NetworkManager
#   - ISC DHCP server to hand out IPs to cameras/switch
#   - Jumbo frames (MTU 9000) and large receive buffers
#
# Usage:  sudo bash setup_network.sh [INTERFACE]
#
# If INTERFACE is omitted, the script auto-detects a 10G NIC.
# ─────────────────────────────────────────────────────────────

SUBNET="192.168.1.0"
STATIC_IP="192.168.1.1"
DHCP_RANGE_START="192.168.1.10"
DHCP_RANGE_END="192.168.1.100"
CONN_NAME="DHCP-Server"

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0 [interface])"
  exit 1
fi

# ── Detect or accept interface ──────────────────────────────
if [ -n "${1:-}" ]; then
  NIC="$1"
  if [ ! -d "/sys/class/net/${NIC}" ]; then
    echo "Error: interface ${NIC} does not exist."
    echo "Available interfaces:"
    ls /sys/class/net/ | grep -v lo
    exit 1
  fi
else
  echo "==> Auto-detecting 10G NIC..."
  NIC=""
  for iface in /sys/class/net/en*; do
    name=$(basename "$iface")
    speed=$(cat "${iface}/speed" 2>/dev/null || echo 0)
    if [ "$speed" -ge 10000 ] 2>/dev/null; then
      NIC="$name"
      break
    fi
  done

  if [ -z "$NIC" ]; then
    echo "Error: no 10G NIC detected. Specify interface manually:"
    echo "  sudo bash $0 <interface>"
    echo ""
    echo "Available interfaces:"
    for iface in /sys/class/net/en*; do
      name=$(basename "$iface")
      speed=$(cat "${iface}/speed" 2>/dev/null || echo "?")
      driver=$(basename "$(readlink "${iface}/device/driver" 2>/dev/null)" 2>/dev/null || echo "?")
      echo "  ${name}  speed=${speed}  driver=${driver}"
    done
    exit 1
  fi
  echo "    Found: ${NIC}"
fi

echo ""
echo "==> Configuring ${NIC} for FLIR camera network"
echo ""

# ── 1. NetworkManager static IP ─────────────────────────────
echo "==> [1/4] Setting static IP ${STATIC_IP}/24 on ${NIC}..."
if nmcli con show "${CONN_NAME}" &>/dev/null; then
  echo "    Updating existing '${CONN_NAME}' profile..."
  nmcli con mod "${CONN_NAME}" connection.interface-name "${NIC}"
  nmcli con mod "${CONN_NAME}" ipv4.method manual
  nmcli con mod "${CONN_NAME}" ipv4.addresses "${STATIC_IP}/24"
else
  echo "    Creating '${CONN_NAME}' profile..."
  nmcli con add type ethernet con-name "${CONN_NAME}" ifname "${NIC}" \
    ipv4.method manual ipv4.addresses "${STATIC_IP}/24"
fi
nmcli con up "${CONN_NAME}"
echo "    Done."

# ── 2. ISC DHCP server ──────────────────────────────────────
echo ""
echo "==> [2/4] Configuring ISC DHCP server..."
apt-get update -qq
apt-get install -y isc-dhcp-server

# Set interface
cat > /etc/default/isc-dhcp-server <<DHCPDEF
INTERFACESv4=${NIC}
INTERFACESv6=""
DHCPDEF

# Write dhcpd.conf
cat > /etc/dhcp/dhcpd.conf <<DHCPCONF
ddns-update-style none;
default-lease-time 600;
max-lease-time 7200;

subnet ${SUBNET} netmask 255.255.255.0 {
    range ${DHCP_RANGE_START} ${DHCP_RANGE_END};
    option domain-name-servers 8.8.8.8, 8.8.4.4;
    option routers ${STATIC_IP};
    option broadcast-address 192.168.1.255;
}
DHCPCONF

systemctl enable isc-dhcp-server
systemctl restart isc-dhcp-server
echo "    DHCP serving ${DHCP_RANGE_START}–${DHCP_RANGE_END} on ${NIC}."

# ── 3. Jumbo frames + receive buffers ───────────────────────
echo ""
echo "==> [3/4] Setting MTU 9000 and socket buffers..."
ip link set "${NIC}" mtu 9000
echo "    MTU now: $(cat /sys/class/net/${NIC}/mtu)"

sysctl -w net.core.rmem_max=10000000 > /dev/null
sysctl -w net.core.rmem_default=10000000 > /dev/null
echo "    rmem_max=10000000, rmem_default=10000000"

# Persist sysctl across reboots
SYSCTL_FILE="/etc/sysctl.d/90-flir-cameras.conf"
cat > "${SYSCTL_FILE}" <<SYSCTL
net.core.rmem_max=10000000
net.core.rmem_default=10000000
SYSCTL
echo "    Persisted to ${SYSCTL_FILE}"

# ── 4. Update .env ──────────────────────────────────────────
echo ""
echo "==> [4/4] Updating .env..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [ -f "${ENV_FILE}" ]; then
  sed -i "s/^NETWORK_INTERFACE=.*/NETWORK_INTERFACE=${NIC}/" "${ENV_FILE}"
  echo "    Set NETWORK_INTERFACE=${NIC} in .env"
else
  echo "    No .env found — copy .env.example and set NETWORK_INTERFACE=${NIC}"
fi

# ── Summary ─────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  Network setup complete"
echo "  Interface:  ${NIC}"
echo "  Static IP:  ${STATIC_IP}/24"
echo "  DHCP range: ${DHCP_RANGE_START} – ${DHCP_RANGE_END}"
echo "  MTU:        9000"
echo "════════════════════════════════════════════════════"
echo ""
echo "Note: MTU 9000 resets on reboot. Either:"
echo "  - Run 'sudo bash set_mtu.sh' after each boot, or"
echo "  - Add 'ethernet.mtu=9000' to the NetworkManager profile:"
echo "    sudo nmcli con mod \"${CONN_NAME}\" 802-3-ethernet.mtu 9000"
