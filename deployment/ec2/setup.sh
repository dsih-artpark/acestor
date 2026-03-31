#!/usr/bin/env bash
# Install Docker on a fresh EC2 instance.
# Supports: Amazon Linux 2023, Ubuntu 22.04 LTS
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash setup.sh)" >&2
  exit 1
fi

. /etc/os-release

case "$ID" in
  amzn)
    echo "Detected Amazon Linux 2023"
    dnf install -y docker
    systemctl enable --now docker
    usermod -aG docker ec2-user
    echo "Done. Log out and back in for docker group to take effect."
    ;;
  ubuntu)
    echo "Detected Ubuntu"
    apt-get update -y
    apt-get install -y docker.io
    systemctl enable --now docker
    usermod -aG docker ubuntu
    echo "Done. Log out and back in for docker group to take effect."
    ;;
  *)
    echo "Unsupported OS: $ID. Install Docker manually." >&2
    exit 1
    ;;
esac
