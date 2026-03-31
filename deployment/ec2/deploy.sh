#!/usr/bin/env bash
# Build the Docker image and start the APScheduler-based pipeline scheduler.
# The scheduler process runs continuously inside the container and fires the
# pipeline on the cron schedule defined in scripts/run_schedules.py.
#
# Usage: bash deployment/ec2/deploy.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "Missing .env in repo root. Copy from deployment/ec2/env.example and fill in values." >&2
  exit 1
fi

# Stop any existing scheduler container before rebuilding
docker stop acestor-scheduler 2>/dev/null || true
docker rm   acestor-scheduler 2>/dev/null || true

echo "Building Docker image..."
docker build -t acestor:latest .

echo "Starting scheduler..."
docker run -d \
  --name acestor-scheduler \
  --restart unless-stopped \
  --env-file .env \
  -v "$ROOT_DIR/configs:/app/configs:ro" \
  -v "$ROOT_DIR/artifacts:/app/artifacts" \
  -v "$ROOT_DIR/logs:/app/logs" \
  acestor:latest \
  uv run python scripts/run_schedules.py

echo "Scheduler started. Logs: docker logs -f acestor-scheduler"
