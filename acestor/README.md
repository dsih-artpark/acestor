# acestor (Python package)

This directory is the **runtime package** for **acestor**—the production dengue pipeline described in the [repository README](../README.md).

The notes below are for **contributors** (tooling, hooks, Makefile targets).

## Development setup

Install dependencies (including dev tools):

```bash
make install-dev
# or: uv sync --all-extras --dev
```

## Pre-commit hooks

Hooks are defined in [`.pre-commit-config.yaml`](.pre-commit-config.yaml). By default they run:

| Hook   | Purpose                          |
| ------ | -------------------------------- |
| **ruff** | Lint (with `--fix` where possible) |
| **black** | Format Python code                 |

### One-time install (register Git hooks)

`pre-commit` is installed only with **dev** dependencies. If you see `Failed to spawn: pre-commit` or “No such file or directory”, run `make install-dev` first (or use the target below, which runs it for you).

```bash
make pre-commit-install
# installs dev deps, then: uv run pre-commit install
```

Without Make, sync then install:

```bash
uv sync --all-extras --dev
uv run pre-commit install
```

This writes a script into `.git/hooks/pre-commit` so hooks run on every `git commit`.

### Run hooks manually

On all tracked files:

```bash
make pre-commit-run
# or: uv run pre-commit run --all-files
```

On staged files only (what a commit would run):

```bash
uv run pre-commit run
```

### Configure or change hooks

1. Edit **`.pre-commit-config.yaml`** — add repos under `repos:`, pin `rev:` (tag or SHA), list `hooks:` with `id:` and optional `args:` / `files:` / `exclude:`.
2. Find more hooks at [pre-commit.com/hooks.html](https://pre-commit.com/hooks.html).
3. After editing the config, update hook environments:

   ```bash
   uv run pre-commit autoupdate   # optional: bump hook versions
   uv run pre-commit install      # re-install if needed
   uv run pre-commit run --all-files
   ```

### Without `make` / `uv`

If you use a plain virtualenv:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Other Makefile targets

| Target          | Command                    |
| --------------- | -------------------------- |
| `lint`          | `uv run ruff check .`      |
| `lint-fix`      | `uv run ruff check . --fix` |
| `format`        | `uv run black .`           |
| `test`          | `uv run pytest`            |
