# acestor

**acestor** is a production dengue intelligence pipeline. It ingests case and weather data, estimates risk thresholds, runs forecasting models, produces maps, and generates report artifacts — all driven from a single YAML config file.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Running the pipeline](#running-the-pipeline)
4. [Scheduling pipelines](#scheduling-pipelines)
5. [Configuration guide](#configuration-guide)
6. [Pipeline stages](#pipeline-stages)
7. [Project layout](#project-layout)
8. [Development](#development)
9. [Config reference](docs/CONFIG_REFERENCE.md)
10. [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## Prerequisites

Before anything else, make sure you have:

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **uv** — fast Python package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **Geospatial system libraries** — required for `geopandas` / `shapely`:
  - **Mac**: `brew install gdal proj geos`
  - **Linux (Debian/Ubuntu)**: `apt-get install gdal-bin libgdal-dev libgeos-dev libproj-dev`
  - **Windows**: install [OSGeo4W](https://trac.osgeo.org/osgeo4w/) or use WSL
- **pdflatex** *(optional)* — only needed if `report.compile_pdf: true` in your config

---

## Installation

**1. Clone the repo**

```bash
git clone https://github.com/dsih-artpark/acestor.git acestor-v2
cd acestor-v2
```

**2. Install dependencies**

```bash
uv sync --extra dengue --extra cds --extra s3
```

| Extra | What it adds |
|-------|-------------|
| `dengue` | Geospatial + modeling stack (geopandas, scikit-learn, etc.) |
| `cds` | Copernicus CDS weather downloads |
| `s3` | AWS S3 storage backend |

**3. Set up environment variables**

Copy the example env file and fill in your secrets:

```bash
cp .env.example .env   # if it exists, otherwise create .env manually
```

At minimum, set these if you use CDS downloads or email notifications:

```
CDS_API_KEY=your-key-here
SMTP_PASSWORD=your-password-here
```

> Secrets in YAML configs use `${VAR:-default}` syntax — never commit real keys.

---

## Running the pipeline

### One-off run (recommended to start with)

```bash
uv run python -m acestor.run \
  --pipeline pipelines.gba_dengue.pipeline:build_pipeline \
  --config configs/gba_docker_test.yaml \
  --run-id my-first-run
```

- `--pipeline` — points to the pipeline builder function
- `--config` — your YAML config file
- `--run-id` — any string to identify this run; outputs go under `{artifacts_base}/{run-id}/`

Exit code `0` = success, non-zero = failure.

### Using Make

```bash
make run-dengue-pipeline DENGUE_RUN_ID=my-run

# With a custom config:
DENGUE_CONFIG=configs/gba_docker_test.yaml make run-dengue-pipeline DENGUE_RUN_ID=my-run

# Incremental/staged graph (faster, for testing):
make run-dengue-pipeline-incremental DENGUE_RUN_ID=smoke-001
```

### Inspecting outputs

Outputs land under `{storages.artifacts.filesystem.base_path}/{run_id}/`:

```
{run_id}/
  predictions/
  plots/
  reports/
  results/     ← zipped LaTeX bundle, maps zip
```

---

## Scheduling pipelines

Use `scripts/run_schedules.py` to run one or more pipelines on a recurring schedule.

### 1. Configure your pipelines

Edit the `PIPELINES` list at the top of [scripts/run_schedules.py](scripts/run_schedules.py):

```python
PIPELINES = [
    {
        "name":     "gba-weekly",
        "cron":     "0 6 * * 1",   # every Monday at 06:00 UTC
        "pipeline": "pipelines.gba_dengue.pipeline:build_pipeline",
        "config":   "configs/gba_stage1_s3.yaml",
    },
    # add more pipelines here
]
```

Cron expression format: `minute  hour  day  month  day_of_week`

| Example | Meaning |
|---------|---------|
| `0 6 * * 1` | Every Monday at 06:00 UTC |
| `0 8 * * *` | Every day at 08:00 UTC |
| `*/30 * * * *` | Every 30 minutes |

### 2. Run the scheduler

**Foreground** (useful for testing):
```bash
uv run python scripts/run_schedules.py
```

**Background — Mac/Linux:**
```bash
nohup uv run python scripts/run_schedules.py > .acestor/scheduler.out 2>&1 &
echo $!   # prints the PID — save it to stop the scheduler later
```

**Background — Windows:**
```powershell
Start-Process pythonw -ArgumentList "scripts\run_schedules.py" -WindowStyle Hidden
```

**Stop the scheduler (Mac/Linux):**
```bash
kill <PID>
```

### 3. View logs

Each run writes its own log file:

```
logs/
  gba-weekly/
    run-20260327_060000.log
    run-20260403_060000.log
```

Watch a run live:
```bash
tail -f logs/gba-weekly/run-20260327_060000.log
```

List all runs for a pipeline:
```bash
ls -lht logs/gba-weekly/
```

> If the scheduler was briefly down and missed a scheduled run, it will catch up automatically (within a 1-hour grace window).

### Deployment

For production deployment on Docker or AWS EC2 — including systemd setup, IAM roles, S3 artifact storage, and log monitoring — see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

---

## Configuration guide

Start from an example config in [`configs/`](configs/) — `gba_docker_test.yaml` is a good starting point.

For a full reference of every config key, see **[docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md)**.

### Key sections to edit

```yaml
pipeline:
  name: dengue
  title: "Dengue Intelligence"   # used in report titles

run:
  run_date: "2026-03-18"         # the reference date for this run

storages:
  artifacts:
    filesystem:
      base_path: "/path/to/outputs"   # where all run outputs are written

data:
  case_download:
    enabled: true
    source_path: "datasets/raw_linelist_data/..."

  geojson:
    base_path: "geojsons/geojsons_GBA"

email:                           # optional — run notifications
  enabled: false
  on: [success, failed]
  smtp_host: smtp.example.com
  to: [you@example.com]

report:
  compile_pdf: false             # set true if pdflatex is installed
```

### Environment variables in YAML

Use `${VAR}` or `${VAR:-default}` anywhere in the config — they are resolved at load time:

```yaml
email:
  smtp_password: "${SMTP_PASSWORD}"
```

---

## Pipeline stages

Steps execute in DAG order. Names match logs and code under `pipelines/gba_dengue/steps/`.

| # | Step | What it does |
|---|------|-------------|
| 1 | `identify_sampling_day` | Resolves the case window and run metadata |
| 2 | `download_case_data` | Fetches/copies case inputs |
| 3 | `download_weather_data` | Fetches/copies weather inputs (CDS or local) |
| 4 | `parse_case_data` | Parses linelist → daily case series by region |
| 5 | `validate_case_data_sufficiency` | Optional gate — stops early if data is too thin |
| 6 | `parse_weather_data` | Aggregates weather features aligned to regions |
| 7 | `identify_cutoff_dates` | Case/weather cutoffs and prediction calendar |
| 8 | `generate_thresholds` | Builds threshold tables from history + config |
| 9 | `train_and_predict` | Fits models, writes predictions |
| 10 | `combine_predictions` | Single combined predictions table |
| 11 | `assess_thresholds` | Threshold assessment + figure metadata |
| 12 | `generate_maps` | Choropleth map PNGs |
| 13 | `generate_report` | JSON + LaTeX bundle + maps zip + optional PDF |
| 14 | `notify_run` | Sends success email (if configured) |

---

## Project layout

```
acestor-v2/
├── acestor/          # Core runtime: config, orchestration, storage, CLI
├── pipelines/        # Pipeline stage implementations and DAG builders
├── configs/          # Example YAML configs
├── scripts/
│   ├── run_schedules.py      # Multi-pipeline scheduler
│   └── install_schedule.py  # Crontab installer (Docker mode)
├── logs/             # Per-run log files (created at runtime)
├── Dockerfile
└── pyproject.toml
```

---

## Development

```bash
# Install with dev extras
uv sync --all-extras

# Lint, format, test
make lint
make format
make test
```

Pre-commit hooks: `pre-commit install`

---

## Contact

For ARTPARK deployments and collaboration: [artpark.in](https://www.artpark.in/)
GitHub: [dsih-artpark/acestor](https://github.com/dsih-artpark/acestor)
Issues: [github.com/dsih-artpark/acestor/issues](https://github.com/dsih-artpark/acestor/issues)
