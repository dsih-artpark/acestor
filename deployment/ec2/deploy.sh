#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "Missing .env in repo root. Create it from deployment/ec2/env.example." >&2
  exit 1
fi

docker build -t acestor:latest .

set -a
source .env
set +a

docker run --rm \
  --name acestor-run \
  --env-file .env \
  -v "$ROOT_DIR/configs:/app/configs" \
  -v "$ROOT_DIR/geojsons:/app/geojsons" \
  -v "$ROOT_DIR/artifacts:/app/artifacts" \
  acestor:latest \
  --pipeline "${PIPELINE_SPEC:-pipelines.gba_dengue.pipeline:build_pipeline}" \
  --config "${CONFIG_PATH:-configs/dengue_s3_env.yaml}" \
  --run-id "${RUN_ID:-ec2-run-001}"
