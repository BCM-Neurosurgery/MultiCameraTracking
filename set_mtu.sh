#!/bin/sh
# Source the environment variables from the .env file
ENV_FILE=".env"

if [ -f "$ENV_FILE" ]; then
  export $(grep -v '^#' "$ENV_FILE" | sed 's/ #.*//g' | grep -v '^$' | xargs)
fi

# Ensure NETWORK_INTERFACE is set before using it
if [ -n "$NETWORK_INTERFACE" ]; then
  sudo ip link set "$NETWORK_INTERFACE" mtu 9000
else
  echo "Error: NETWORK_INTERFACE is not set in .env"
  exit 1
fi

sysctl -w net.core.rmem_max=10000000
sysctl -w net.core.rmem_default=10000000
