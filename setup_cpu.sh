#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# setup_cpu.sh — Install Docker for CPU-only MultiCameraTracking
# deployments. Skips all NVIDIA driver / NVENC / container-toolkit
# steps performed by setup_gpu.sh.
#
# Use this on hosts without an NVIDIA GPU. Acquisition + FastAPI
# backend + React frontend work on CPU; analysis pipeline does not
# (see README → Deployment Profiles).
#
# Usage:  sudo bash setup_cpu.sh
# ─────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0)"
  exit 1
fi

# ── 1. Warn if a GPU is present ─────────────────────────────
echo ""
echo "==> [1/3] Checking host environment..."
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
  echo "    Notice: NVIDIA GPU detected on this host."
  echo "    setup_cpu.sh installs Docker only. If you want the GPU profile"
  echo "    (analysis pipeline + NVENC), run setup_gpu.sh instead."
else
  echo "    No NVIDIA GPU detected — CPU profile is the right choice."
fi

# ── 2. Docker ───────────────────────────────────────────────
echo ""
echo "==> [2/3] Checking Docker..."
if command -v docker &>/dev/null; then
  echo "    Docker $(docker --version | awk '{print $3}') already installed — skipping."
else
  echo "    Installing Docker..."
  apt-get update -qq
  apt-get install -y docker.io docker-compose-plugin
  systemctl enable --now docker
  echo "    Installed."
fi

# ── 3. Verify ───────────────────────────────────────────────
echo ""
echo "==> [3/3] Verifying Docker..."

PASS=true

if docker compose version &>/dev/null; then
  echo "    docker compose        ✓ ($(docker compose version --short))"
else
  echo "    docker compose        ✗"
  PASS=false
fi

if docker run --rm hello-world &>/dev/null; then
  echo "    Docker runtime        ✓"
else
  echo "    Docker runtime        ✗"
  PASS=false
fi

echo ""
if $PASS; then
  echo "All good! Run 'make build && make validate' next."
  echo "PROFILE is auto-detected by the Makefile; on this host it will be 'cpu'."
else
  echo "Some checks failed — see above."
fi
