#!/usr/bin/env bash
# Set up the pipeline natively on EC2 — no Docker required.
# Supports: Amazon Linux 2023, Ubuntu 22.04 LTS
#
# Run once from the repo root:
#   sudo bash deployment/ec2-native/setup.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash deployment/ec2-native/setup.sh)" >&2
  exit 1
fi

. /etc/os-release
DEPLOY_USER="${SUDO_USER:-$(logname 2>/dev/null || echo ec2-user)}"
HOME_DIR=$(eval echo "~$DEPLOY_USER")

echo "==> Installing system dependencies for: $ID"

case "$ID" in
  amzn)
    dnf install -y \
      python3.12 python3.12-devel \
      gcc gcc-c++ make git \
      gdal gdal-devel \
      geos geos-devel \
      proj proj-devel \
      spatialindex spatialindex-devel \
      texlive texlive-latex texlive-xetex \
      texlive-collection-latexextra \
      curl
    ;;
  ubuntu)
    apt-get update -y
    apt-get install -y \
      python3.12 python3.12-venv python3.12-dev \
      build-essential git curl \
      gdal-bin libgdal-dev \
      libgeos-dev \
      libproj-dev proj-bin proj-data \
      libspatialindex-dev \
      texlive-latex-base texlive-latex-extra texlive-fonts-recommended
    ;;
  *)
    echo "Unsupported OS: $ID" >&2
    exit 1
    ;;
esac

echo "==> Installing uv for $DEPLOY_USER"
sudo -u "$DEPLOY_USER" bash -c \
  'curl -LsSf https://astral.sh/uv/install.sh | sh'

UV_BIN="$HOME_DIR/.local/bin/uv"

echo "==> Installing Python project dependencies"
cd /opt/acestor
sudo -u "$DEPLOY_USER" "$UV_BIN" sync --frozen --extra dengue --extra cds --extra s3

echo ""
echo "Setup complete."
echo "Next: copy deployment/ec2-native/env.example to /opt/acestor/.env and fill in values."
echo "Then: sudo systemctl enable --now acestor.timer"
