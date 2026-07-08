#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export POSTGRES_URL="${POSTGRES_URL:-postgresql+psycopg://acestor:acestor@localhost:5432/acestor}"
export S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-http://localhost:9000}"
export S3_BUCKET="${S3_BUCKET:-acestor-test}"
export S3_REGION="${S3_REGION:-us-east-1}"
export S3_ACCESS_KEY="${S3_ACCESS_KEY:-minioadmin}"
export S3_SECRET_KEY="${S3_SECRET_KEY:-minioadmin}"
export S3_USE_PATH_STYLE=true
export AUTH_PROVIDERS=local
export AUTH_SESSION_SECRET=test

docker compose -f docker-compose.dev.yml up -d postgres minio

echo "waiting for postgres..."
until docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U acestor >/dev/null 2>&1; do
  sleep 1
done

echo "waiting for minio..."
until curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1; do
  sleep 1
done

uv sync --all-extras
uv run pytest -v "$@"
