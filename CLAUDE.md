# acestor-v2 — Claude Code Context

## Project

Python dengue forecasting pipeline for ARTPARK / IISc. Runs via `uv run python -m acestor.run`. Tests via `uv run pytest` (bare `pytest` fails — pyproject.toml coverage flags require uv).

## pr-verifier data

Data directories required by the pipeline are gitignored. They live at these absolute paths on the dev machine:

```
prepared_data/     → ./prepared_data  (relative to project root — created by dengue_prep pipeline)
ap_datasets/       → ./ap_datasets    (geojsons + raw AP data)
```

If those relative paths don't exist in a fresh clone, the agent should ask the user where they are rather than guessing. The user may have them at an absolute path like `~/data/ap/` or similar.

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
