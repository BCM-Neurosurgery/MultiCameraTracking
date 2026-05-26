#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo bash /tmp/disable-auto-updates-ubuntu.sh" >&2
  exit 1
fi

units=(
  apt-daily.timer
  apt-daily.service
  apt-daily-upgrade.timer
  apt-daily-upgrade.service
  unattended-upgrades.service
  update-notifier-download.timer
  update-notifier-download.service
  update-notifier-motd.timer
  update-notifier-motd.service
  fwupd-refresh.timer
  fwupd-refresh.service
  ua-timer.timer
  ua-timer.service
  motd-news.timer
  motd-news.service
  snapd.snap-repair.timer
  snapd.snap-repair.service
)

systemctl mask --now "${units[@]}"

install -m 0644 /dev/stdin /etc/apt/apt.conf.d/99disable-auto-updates <<'APTCONF'
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::CleanInterval "0";
Unattended-Upgrade::Automatic-Reboot "false";
APTCONF

if [ -f /etc/update-manager/release-upgrades ]; then
  cp -n /etc/update-manager/release-upgrades /etc/update-manager/release-upgrades.bak-disable-auto-updates
  if grep -q '^Prompt=' /etc/update-manager/release-upgrades; then
    sed -i 's/^Prompt=.*/Prompt=never/' /etc/update-manager/release-upgrades
  else
    printf '\nPrompt=never\n' >> /etc/update-manager/release-upgrades
  fi
fi

if command -v snap >/dev/null 2>&1; then
  snap refresh --hold
fi

systemctl daemon-reload

echo "Automatic update mechanisms found on this Ubuntu system are disabled."
echo "Manual updates still work with apt, snap, or the GUI when you choose to run them."
