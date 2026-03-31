#!/usr/bin/env bash
# Start the APScheduler-based pipeline scheduler (no Docker).
# Runs continuously — manage with systemd (see acestor.service).
#
# Usage: bash deployment/ec2-native/run.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "Missing .env in repo root. Copy from deployment/ec2-native/env.example and fill in values." >&2
  exit 1
fi

set -a; source .env; set +a

exec ~/.local/bin/uv run python scripts/run_schedules.py
