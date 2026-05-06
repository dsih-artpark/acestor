# acestor-v2 — Claude Code Context

## Project

Python dengue forecasting pipeline for ARTPARK / IISc. Runs via `uv run python -m acestor.run`. Tests via `uv run pytest` (bare `pytest` fails — pyproject.toml coverage flags require uv).

Repository URL: `https://github.com/dsih-artpark/acestor.git`

## pr-verifier data

Raw input data for the prep pipeline. Gitignored — lives relative to the project root on the dev machine:

```
ap_datasets/geojsons/geojsons_AP   — district geojsons (read-only)
ap_datasets/raw_case/              — raw IHIP case files (.xlsx/.xls/.csv, read-only)
```

Absolute path on this machine: `/Users/ashutoshsinghai/Desktop/ARTPARK/acestor-v2/ap_datasets`

`prepared_data/` is pipeline OUTPUT — the prep step generates it fresh; it is never a pre-existing input.
Weather is downloaded at prep-time from the openmeteo API — no local files needed.

The pr-verifier patches configs with the absolute `ap_datasets` path before running — no symlinks, no copies.

Prep pipeline config: `configs/ap_district_prep.yaml`
Prep pipeline entry: `pipelines.dengue_prep.pipeline:build_pipeline`

## pr-verifier configs

Three configs exercise all output modes against AP data:

| Config | output | primary |
|--------|--------|---------|
| `configs/ap_district.yaml` | ensemble (default) | ensemble |
| `configs/ap_district_both.yaml` | both | ensemble |
| `configs/ap_district_per_model.yaml` | per_model | nbr |

## pr-verifier ports

No web server. Pipeline runs are isolated by `--run-id`. No port conflicts possible.

## Dependency installation

Always `uv sync --all-extras` immediately after cloning. Never install packages one by one — if an import fails after sync, it's a lockfile issue, not a missing package.

## Run commands

```bash
# Main pipeline
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config <config.yaml> \
  --run-id <run-id>

# Prep pipeline
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml \
  --run-id <run-id>

# Tests
uv run pytest tests/ -v
```
