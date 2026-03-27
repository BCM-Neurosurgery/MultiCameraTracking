#!/usr/bin/env bash
set -euo pipefail

# Install NVIDIA Container Toolkit so Docker can access the GPU.
# Usage: sudo bash install_nvidia_container_toolkit.sh

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: must run as root (sudo bash $0)"
  exit 1
fi

echo "==> Adding NVIDIA container toolkit repo..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg --yes

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

echo "==> Installing nvidia-container-toolkit..."
apt-get update -qq
apt-get install -y nvidia-container-toolkit

echo "==> Configuring Docker runtime..."
nvidia-ctk runtime configure --runtime=docker

echo "==> Restarting Docker..."
systemctl restart docker

echo "==> Verifying GPU access in Docker..."
if docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi > /dev/null 2>&1; then
  echo "Success! Docker can access the GPU."
else
  echo "Warning: verification failed. Check 'docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi' manually."
fi
