#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# setup_persistence.sh — Enable NVIDIA persistence mode across
# reboots by overriding the shipped nvidia-persistenced.service.
#
# Ubuntu's NVIDIA driver package ships the systemd unit with
# --no-persistence-mode on the ExecStart line, which makes the
# daemon run as a no-op (GPU reports "Persistence Mode: Disabled"
# even while the service is "active (running)").
#
# Without persistence mode, the NVIDIA driver tears down CUDA
# runtime state during brief no-client windows — which our
# encoder pipeline creates at every segment boundary when all
# ffmpeg processes close simultaneously. The next ffmpeg's
# cuInit(0) can fail with CUDA_ERROR_NO_DEVICE and abort the
# recording. See bugs/incident_20260423_cuda_no_device_segment_boundary.md.
#
# This script installs a systemd drop-in override that strips
# the --no-persistence-mode flag, so the daemon actually enables
# persistence on each boot. Safe to re-run; idempotent.
#
# Usage:  sudo bash setup_persistence.sh
# ─────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0)"
  exit 1
fi

OVERRIDE_DIR="/etc/systemd/system/nvidia-persistenced.service.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/override.conf"

# ── 1. Preconditions ────────────────────────────────────────
echo ""
echo "==> [1/5] Checking preconditions..."

if ! command -v nvidia-smi &>/dev/null; then
  echo "    nvidia-smi not found — is the NVIDIA driver installed?"
  echo "    Run setup_gpu.sh first."
  exit 1
fi

if ! nvidia-smi &>/dev/null; then
  echo "    nvidia-smi present but cannot query GPU."
  echo "    Check the driver is loaded: lsmod | grep nvidia"
  exit 1
fi

if ! systemctl list-unit-files nvidia-persistenced.service &>/dev/null; then
  echo "    nvidia-persistenced.service not installed."
  echo "    It ships with the NVIDIA driver package. Reinstall the driver?"
  exit 1
fi

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
echo "    GPU:    ${GPU_NAME}"
echo "    Driver: ${DRIVER_VER}"

# ── 2. Current state ────────────────────────────────────────
echo ""
echo "==> [2/5] Checking current persistence state..."

CURRENT_PM=$(nvidia-smi -q | awk -F': ' '/Persistence Mode/ {print $2; exit}')
CURRENT_EXECSTART=$(systemctl show nvidia-persistenced.service -p ExecStart --value | tr -d '\n')

echo "    Reported by nvidia-smi: ${CURRENT_PM:-unknown}"
if echo "${CURRENT_EXECSTART}" | grep -q "\-\-no-persistence-mode"; then
  echo "    Service ExecStart contains --no-persistence-mode (needs override)"
elif [ -f "${OVERRIDE_FILE}" ] && [ "${CURRENT_PM}" = "Enabled" ]; then
  echo "    Override already installed and persistence is Enabled — nothing to do."
  echo ""
  echo "Verification:"
  echo "  systemctl cat nvidia-persistenced"
  echo "  nvidia-smi -q | grep -i Persistence"
  exit 0
else
  echo "    No --no-persistence-mode flag detected, but persistence is not Enabled."
  echo "    Will still install override to be explicit and idempotent."
fi

# ── 3. Install override ─────────────────────────────────────
echo ""
echo "==> [3/5] Installing systemd override..."

mkdir -p "${OVERRIDE_DIR}"

# Use --persistence-mode explicitly so this works regardless of which
# default the daemon itself compiles with (older drivers defaulted OFF,
# newer ones default ON). Redundant when the default is ON; correct when
# it's OFF. Either way, persistence is explicitly enabled.
cat > "${OVERRIDE_FILE}" <<'EOF'
# Installed by MultiCameraTracking/install/setup_persistence.sh.
# Overrides the shipped nvidia-persistenced unit to strip
# --no-persistence-mode and force persistence ON.
#
# Why: without persistence, the NVIDIA driver tears down CUDA
# state during brief no-client windows. Our encoder pipeline
# creates such windows at every segment boundary when all
# per-camera ffmpeg processes close before the next batch
# opens. The next cuInit(0) can fail with CUDA_ERROR_NO_DEVICE
# and kill the recording. See
# bugs/incident_20260423_cuda_no_device_segment_boundary.md.
[Service]
ExecStart=
ExecStart=/usr/bin/nvidia-persistenced --user nvidia-persistenced --persistence-mode --verbose
EOF

echo "    Wrote ${OVERRIDE_FILE}"

systemctl daemon-reload
echo "    systemctl daemon-reload done"

# ── 4. Restart the daemon (safely) ──────────────────────────
echo ""
echo "==> [4/5] Restarting nvidia-persistenced..."

# Restarting the daemon is documented to be safe even with active
# CUDA clients (the daemon only manages the no-client-window policy;
# it doesn't own live contexts). But if a recording is mid-flight,
# there's no reason to perturb it — the override already takes effect
# on the next service restart or reboot. So: skip the live restart if
# the recording pipeline's ffmpeg encoders are running.
#
# Narrow check on purpose — checking all CUDA clients would also flag
# desktop processes like gnome-remote-desktop-daemon (C+G), which
# don't matter and would cause spurious skips on any desktop system.
RECORDING_FFMPEG=$(nvidia-smi --query-compute-apps=process_name --format=csv,noheader 2>/dev/null | grep -i ffmpeg || true)
if [ -n "${RECORDING_FFMPEG}" ]; then
  echo ""
  echo "    ⚠  ffmpeg encoder processes are currently using the GPU:"
  nvidia-smi --query-compute-apps=pid,process_name --format=csv | grep -iE "pid|ffmpeg"
  echo ""
  echo "    Looks like a recording is in progress. The override is"
  echo "    installed and will take effect on the next service restart"
  echo "    (or the next reboot). Skipping live restart to avoid"
  echo "    disturbing the recording."
  echo ""
  echo "    After the current recording finishes, run:"
  echo "      sudo systemctl restart nvidia-persistenced"
  echo "      nvidia-smi -q | grep -i Persistence   # should read Enabled"
  exit 0
fi

systemctl restart nvidia-persistenced
echo "    Service restarted."

# Give the daemon a beat to set persistence on the device.
sleep 1

# ── 5. Verify ───────────────────────────────────────────────
echo ""
echo "==> [5/5] Verifying..."

PASS=true

ACTIVE_STATE=$(systemctl is-active nvidia-persistenced || true)
if [ "${ACTIVE_STATE}" = "active" ]; then
  echo "    Service state            ✓ active"
else
  echo "    Service state            ✗ ${ACTIVE_STATE}"
  PASS=false
fi

NEW_EXECSTART=$(systemctl show nvidia-persistenced.service -p ExecStart --value | tr -d '\n')
if echo "${NEW_EXECSTART}" | grep -q "\-\-no-persistence-mode"; then
  echo "    ExecStart flag           ✗ still contains --no-persistence-mode"
  PASS=false
else
  echo "    ExecStart flag           ✓ --no-persistence-mode removed"
fi

NEW_PM=$(nvidia-smi -q | awk -F': ' '/Persistence Mode/ {print $2; exit}')
if [ "${NEW_PM}" = "Enabled" ]; then
  echo "    nvidia-smi reports       ✓ Persistence Mode: Enabled"
else
  echo "    nvidia-smi reports       ✗ Persistence Mode: ${NEW_PM}"
  PASS=false
fi

echo ""
if $PASS; then
  echo "Persistence mode is now enabled and will survive reboots."
  echo "Run 'nvidia-smi -q | grep -i Persistence' any time to verify."
else
  echo "One or more checks failed. Inspect:"
  echo "  systemctl status nvidia-persistenced"
  echo "  journalctl -u nvidia-persistenced --since '5 minutes ago'"
  exit 1
fi
