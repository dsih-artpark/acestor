PYTHON ?= python

.PHONY: install-dev lint format lint-fix pre-commit-install pre-commit-run test \
	run-dengue-pipeline run-dengue-pipeline-incremental

install-dev:
	uv sync --all-extras --dev

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check . --fix

format:
	uv run black .

# Depends on install-dev: pre-commit is an optional dev extra; without sync, `uv run pre-commit` fails.
pre-commit-install: install-dev
	uv run pre-commit install

pre-commit-run: install-dev
	uv run pre-commit run --all-files

test:
	uv run pytest

# --- Dengue production pipeline (see README) ---
DENGUE_CONFIG ?= configs/stage1.yaml
DENGUE_RUN_ID ?= local

run-dengue-pipeline:
	uv run python -m acestor.run \
	  --pipeline pipelines.dengue.pipeline:build_pipeline \
	  --config $(DENGUE_CONFIG) \
	  --run-id $(DENGUE_RUN_ID)

run-dengue-pipeline-incremental:
	uv run python -m acestor.run \
	  --pipeline pipelines.dengue.pipeline_incremental:build_pipeline \
	  --config $(DENGUE_CONFIG) \
	  --run-id $(DENGUE_RUN_ID)

