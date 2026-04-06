#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# setup_gpu.sh — Install NVIDIA driver, container toolkit, and
# NVENC libraries for Docker-based GPU workloads.
#
# Usage:  sudo bash setup_gpu.sh
# ─────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0)"
  exit 1
fi

# ── 1. NVIDIA driver ────────────────────────────────────────
echo ""
echo "==> [1/5] Checking NVIDIA driver..."
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
  DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
  echo "    Driver ${DRIVER_VER} already installed — skipping."
else
  echo "    No working driver found. Installing nvidia-driver-570-server..."
  apt-get update -qq
  apt-get install -y nvidia-driver-570-server
  echo "    Driver installed. A REBOOT is required before continuing."
  echo "    Run this script again after rebooting."
  exit 0
fi

# ── 2. NVENC library ────────────────────────────────────────
echo ""
echo "==> [2/5] Checking NVENC encode library..."
DRIVER_MAJOR=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | cut -d. -f1)
ENCODE_PKG="libnvidia-encode-${DRIVER_MAJOR}-server"

if ldconfig -p 2>/dev/null | grep -q libnvidia-encode; then
  echo "    libnvidia-encode already present — skipping."
else
  echo "    Installing ${ENCODE_PKG}..."
  apt-get update -qq
  apt-get install -y "${ENCODE_PKG}"
  echo "    Installed."
fi

# ── 3. Docker ───────────────────────────────────────────────
echo ""
echo "==> [3/5] Checking Docker..."
if command -v docker &>/dev/null; then
  echo "    Docker $(docker --version | awk '{print $3}') already installed — skipping."
else
  echo "    Installing Docker..."
  apt-get update -qq
  apt-get install -y docker.io docker-compose-plugin
  systemctl enable --now docker
  echo "    Installed."
fi

# ── 4. NVIDIA Container Toolkit ─────────────────────────────
echo ""
echo "==> [4/5] Installing NVIDIA Container Toolkit..."
if dpkg -l nvidia-container-toolkit &>/dev/null; then
  echo "    nvidia-container-toolkit already installed — reconfiguring."
else
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg --yes

  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

  apt-get update -qq
  apt-get install -y nvidia-container-toolkit
fi

nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# ── 5. Verify ───────────────────────────────────────────────
echo ""
echo "==> [5/5] Verifying Docker GPU + NVENC access..."

PASS=true

if docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi &>/dev/null; then
  echo "    Docker GPU access     ✓"
else
  echo "    Docker GPU access     ✗"
  PASS=false
fi

if docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 \
    bash -c 'ls /usr/lib/x86_64-linux-gnu/libnvidia-encode.so.1 2>/dev/null || ls /usr/lib/libnvidia-encode.so.1 2>/dev/null' &>/dev/null; then
  echo "    NVENC in container    ✓"
else
  echo "    NVENC in container    ✗  (libnvidia-encode.so.1 not mounted)"
  PASS=false
fi

echo ""
if $PASS; then
  echo "All good! Run 'make build && make validate' next."
else
  echo "Some checks failed — see above."
fi
