#!/usr/bin/env bash
# Hindcast GBA zone — last 4 months. First Monday tunes, rest reuse the cache.
set -euo pipefail

CONFIG="configs/gba_zone.yaml"
OUT="./artifacts/gba_zone_hindcast/_shared"

# First run — Optuna retune, writes hp cache.
FIRST="2026-03-02"
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config "$CONFIG" --run-id "hindcast_${FIRST//-/}" --clean \
  --set "run.run_date=$FIRST" \
  --set "storages.artifacts.filesystem.base_path=$OUT" \
  --set "model_configs.rf.tune=true" \
  --set "model_configs.xgb.tune=true"

# Remaining Mondays — cache reuse, no retune.
for d in 2026-03-09 2026-03-16 2026-03-23 2026-03-30 \
         2026-04-06 2026-04-13 2026-04-20 2026-04-27 \
         2026-05-04 2026-05-11 2026-05-18 2026-05-25 \
         2026-06-01 2026-06-08 2026-06-15 2026-06-22; do
  uv run python -m acestor.run \
    --pipeline pipelines.dengue.pipeline:build_pipeline \
    --config "$CONFIG" --run-id "hindcast_${d//-/}" --clean \
    --set "run.run_date=$d" \
    --set "storages.artifacts.filesystem.base_path=$OUT" \
    --set "model_configs.rf.tune=never" \
    --set "model_configs.xgb.tune=never"
done
