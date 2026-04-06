#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# install_firefox.sh — Replace Ubuntu's snap Firefox with the
# official Mozilla APT package so that systemd-run memory
# limits work correctly.
#
# Why: Snap Firefox runs in its own cgroup, ignoring any
# MemoryMax set via systemd-run.  The APT version runs as a
# normal process that respects cgroup limits.
#
# Ref: https://www.omgubuntu.co.uk/2022/04/how-to-install-firefox-deb-apt-ubuntu-22-04
#
# Usage:  sudo bash install_firefox.sh
# ─────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0)"
  exit 1
fi

echo ""
echo "==> [1/6] Removing snap Firefox (if installed)..."
if snap list firefox &>/dev/null; then
  snap remove firefox
  echo "    Snap Firefox removed."
else
  echo "    Snap Firefox not installed — skipping."
fi

echo ""
echo "==> [2/6] Removing transitional APT shim (if installed)..."
if dpkg -l firefox 2>/dev/null | grep -q "1snap1"; then
  apt-get remove -y firefox
  echo "    Shim removed."
else
  echo "    No shim found — skipping."
fi

echo ""
echo "==> [3/6] Importing Mozilla APT signing key..."
install -d -m 0755 /etc/apt/keyrings
wget -q https://packages.mozilla.org/apt/repo-signing-key.gpg -O- \
  | tee /etc/apt/keyrings/packages.mozilla.org.asc > /dev/null
echo "    Key imported."

echo ""
echo "==> [4/6] Adding Mozilla APT repository..."
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
  | tee /etc/apt/sources.list.d/mozilla.list > /dev/null
echo "    Repository added."

echo ""
echo "==> [5/6] Pinning Mozilla repo over Ubuntu snap shim..."
cat > /etc/apt/preferences.d/mozilla <<'PIN'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000

Package: firefox*
Pin: release o=Ubuntu
Pin-Priority: -1
PIN
echo "    Pin file written."

echo ""
echo "==> [6/6] Installing Firefox from Mozilla APT repo..."
apt-get update -qq
apt-get install -y firefox
echo "    Installed."

echo ""
echo "════════════════════════════════════════════════════"
VER=$(firefox --version 2>/dev/null || echo "unknown")
echo "  Installed: ${VER}"
echo ""
echo "  To launch with an 8 GB memory cap:"
echo "    systemd-run --user --scope -p MemoryMax=8G firefox"
echo "════════════════════════════════════════════════════"
