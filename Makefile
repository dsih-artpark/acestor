PYTHON ?= python

.PHONY: install-dev lint format lint-fix pre-commit-install pre-commit-run test

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

