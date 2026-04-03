# Contributing to acestor

Thanks for your interest in contributing. This document covers how to set up for development, the branching model, and what to check before submitting a pull request.

---

## Getting started

**1. Fork and clone**

```bash
git clone https://github.com/dsih-artpark/acestor.git
cd acestor
```

**2. Install all extras (including dev tools)**

```bash
uv sync --all-extras
```

**3. Install pre-commit hooks**

```bash
pre-commit install
```

---

## Branching

- `production` — stable, deployed branch. Do not push directly.
- Feature branches should be cut from `production` and named descriptively, e.g. `fix/weather-cache` or `feat/new-region-type`.

---

## Before submitting a PR

Run the full check suite locally:

```bash
make lint      # ruff linting
make format    # ruff formatting
make test      # pytest
```

All checks must pass. The pre-commit hooks will catch most issues on commit.

---

## Pull request guidelines

- Keep PRs focused — one logical change per PR.
- Include a clear description of what changed and why.
- If your change affects pipeline behaviour, test it against a real config (e.g. `configs/gba_docker_test.yaml`) before opening the PR.
- Do not commit `.env` files or any secrets. Use `${VAR}` substitution in YAML configs.

---

## Adding a new pipeline stage

1. Add the step module under `pipelines/dengue/steps/`.
2. Register it in the DAG builder in `pipelines/dengue/pipeline.py`.
3. Document it in the pipeline stages table in `README.md`.

---

## Adding a new region type

Region-specific logic lives in `pipelines/dengue/lib/`. Config keys are documented in [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md).

---

## Reporting issues

Open an issue at [github.com/dsih-artpark/acestor/issues](https://github.com/dsih-artpark/acestor/issues) with:

- The config file (redact any secrets)
- The relevant log output
- Steps to reproduce

---

## Contact

For questions about ARTPARK deployments or collaboration: [artpark.in](https://www.artpark.in/)
