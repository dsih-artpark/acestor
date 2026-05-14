# Analyst Tools — Running Pipelines & Evaluating Forecasts

Three scripts live in `scripts/` for everyday use by analysts and data scientists:

| Script | Purpose |
|---|---|
| `scripts/sync_data.py` | Download / upload pipeline input data from S3 (run this first on a fresh clone) |
| `scripts/run.py` | Run the forecast pipeline with any config overrides from the command line |
| `scripts/compute_backtest_metrics.py` | Evaluate past forecast runs against observed case data |

---

## Prerequisites

All commands below assume you are in the project root (`acestor-v2/`) and have run:

```bash
uv sync --all-extras
```

---

## 0. Getting the data (`sync_data.py`)

Before you can run anything, you need the input datasets locally. They live in S3 and are not checked into git.

### One-time setup

**1. Configure AWS credentials** (skip if already done):

```bash
aws configure   # fill in access key, secret, region: ap-south-1
```

Or set environment variables:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-south-1
```

**2. Set the S3 bucket** (add to `~/.zshrc` or `~/.bashrc`):

```bash
export ACESTOR_S3_BUCKET=artpark-1health-data-dumps
```

### Download everything (new teammate setup)

```bash
uv run python scripts/sync_data.py download
```

This pulls all four datasets into the right local folders:

| Dataset | Local folder | Description |
|---|---|---|
| `cases` | `ap_datasets/raw_case/` | Raw IHIP case files (.xlsx / .csv) |
| `geojsons` | `ap_datasets/geojsons/geojsons_AP/` | AP district boundary GeoJSONs |
| `weather` | `ap_datasets/weather/` | Raw weather CSVs by month |
| `prepared` | `prepared_data/district/` | Prepared cases + weather CSVs |

Files that already exist locally with the same size are skipped automatically.

### Download only what you need

```bash
# Cases only
uv run python scripts/sync_data.py download --only cases

# Weather only
uv run python scripts/sync_data.py download --only weather

# Prepared data only (skip raw inputs)
uv run python scripts/sync_data.py download --only prepared
```

### Preview before downloading

```bash
uv run python scripts/sync_data.py download --dry-run
```

Shows exactly what would be transferred without touching any files.

### Upload new data to S3

After adding new raw case files or updating geojsons:

```bash
uv run python scripts/sync_data.py upload

# Or target a specific dataset
uv run python scripts/sync_data.py upload --only cases
```

### All options

```
uv run python scripts/sync_data.py {download,upload} [OPTIONS]

--bucket NAME       Override the S3 bucket (default: ACESTOR_S3_BUCKET env var)
--only DATASET      Sync only one dataset: cases | geojsons | weather | prepared
--backend BACKEND   Transfer backend: boto3 (default, no CLI needed) | awscli
--dry-run           Show what would be transferred without doing it
```

> **No AWS CLI required.** The default backend uses the `boto3` Python library, which reads the same credentials as the CLI. Install it with `uv sync --all-extras` — it's already in the project dependencies.

---

## 1. Running the pipeline

There are two ways to run the forecast pipeline.

---

### Option A — Original command (full control)

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district_v3.yaml \
  --run-id my-run-name
```

- `--config` — path to the YAML config file
- `--run-id` — name for this run's artifact folder under `artifacts/ap/`; auto-generated if omitted
- `--pipeline` — entry point; use `pipelines.dengue.pipeline:build_pipeline` for the main forecast

To run the data prep pipeline first (downloads and prepares case + weather data):

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml
```

Artifacts land in `artifacts/ap/<run-id>/` with subfolders `results/`, `plots/`, `reports/`, `datasets/`, `thresholds/`.

---

### Option B — `scripts/run.py` (with config overrides)

Use this when you want to tweak config values without editing any YAML file.

```bash
uv run python scripts/run.py [--config FILE] [--run-id ID] [--set KEY=VALUE ...] [--dry-run]
```

**Arguments:**

| Argument | Default | Description |
|---|---|---|
| `--config` | **required** | Base config file to start from |
| `--run-id` | auto-generated | Name for the artifact folder |
| `--set KEY=VALUE` | — | Override any config value (repeatable) |
| `--dry-run` | off | Print the final config and command without running |

**Dotted-key notation**

Keys follow the YAML structure with `.` as the separator. For example:

```
thresholds.method_configs.prev_nweeks.n_weeks=8
```

maps to:

```yaml
thresholds:
  method_configs:
    prev_nweeks:
      n_weeks: 8
```

**Value parsing**

Values are parsed automatically — no need to quote types:

| You type | Parsed as |
|---|---|
| `4` | integer |
| `2.5` | float |
| `true` / `false` | boolean |
| `2026-03-13` | date string |
| `"[nbr, xgb]"` | list |
| `"[]"` | empty list |

---

#### Examples

```bash
# --config is required
uv run python scripts/run.py --config configs/ap_district_v3.yaml

# Change the run date
uv run python scripts/run.py --config configs/ap_district_v3.yaml --set run.run_date=2026-03-13

# Change the rolling window for the prev_nweeks threshold method
uv run python scripts/run.py \
  --set thresholds.method_configs.prev_nweeks.n_weeks=8

# Use fewer years of history for the historical threshold
uv run python scripts/run.py \
  --set thresholds.method_configs.historical.historical_n_years=2

# Run only NBR and XGB (skip Random Forest)
uv run python scripts/run.py --set "model.models=[nbr, xgb]"

# Give the run a specific ID so artifacts land in artifacts/ap/my-experiment/
uv run python scripts/run.py --run-id my-experiment

# Multiple overrides at once
uv run python scripts/run.py \
  --run-id march-sensitivity \
  --set run.run_date=2026-03-13 \
  --set thresholds.method_configs.prev_nweeks.n_weeks=6 \
  --set thresholds.method_configs.historical.historical_n_years=2 \
  --set "model.models=[nbr, xgb]" \
  --set model.output=per_model

# Preview the final config before actually running
uv run python scripts/run.py \
  --set run.run_date=2026-03-13 \
  --set thresholds.method_configs.prev_nweeks.n_weeks=8 \
  --dry-run
```

`--dry-run` prints the fully-resolved config YAML and the exact command that *would* be run — useful for sanity-checking before committing to a long run.

---

### Key config paths (quick reference)

These are the values most commonly overridden:

| What you want to change | Dotted key | Example value |
|---|---|---|
| Run date | `run.run_date` | `2026-03-13` |
| Rolling window (prev_nweeks) | `thresholds.method_configs.prev_nweeks.n_weeks` | `8` |
| Years of history (historical) | `thresholds.method_configs.historical.historical_n_years` | `2` |
| Years to exclude from training | `thresholds.method_configs.historical.excluded_years` | `"[2020, 2021]"` |
| Models to run | `model.models` | `"[nbr, xgb]"` |
| Ensemble strategy | `model.ensemble` | `mean` or `none` |
| Output mode | `model.output` | `ensemble`, `per_model`, or `both` |
| Threshold classification | `thresholds.classification_method` | `who` or `icmr` |
| Threshold methods to run | `thresholds.methods` | `"[historical, prev_nweeks]"` |

---

## 2. Evaluating forecast accuracy (`compute_backtest_metrics.py`)


This script compares past forecast runs against observed case data and produces:

- `metrics_report.html` — interactive visual report (open in browser)
- `metrics_summary.csv` — one row per (run × model × threshold method)
- `metrics_per_week.csv` — accuracy broken down by forecast week
- `metrics_per_district.csv` — accuracy broken down by district
- `metrics.json` — all of the above as JSON

**How actuals are matched**

Each run's predictions cover weeks *after* the run date. The script automatically finds the most recent observed case data across all artifact directories and uses that as ground truth. No manual setup needed — just run it.

---

### Usage

```bash
uv run python scripts/compute_backtest_metrics.py [OPTIONS]
```

| Argument | Default | Description |
|---|---|---|
| `--runs` | all in `DEFAULT_RUNS` | Run directories to evaluate (name, relative path, or absolute) |
| `--models` | all four | Which models to evaluate: `nbr` `rf` `xgb` `ensemble` |
| `--method` | both | Threshold method: `historical` or `previousNweeks` |
| `--output` | `metrics_output/` | Where to write output files |

---

### Examples

```bash
# Evaluate all default runs, open metrics_output/metrics_report.html
uv run python scripts/compute_backtest_metrics.py

# Evaluate a single run (by name — relative to artifacts/ap/)
uv run python scripts/compute_backtest_metrics.py --runs backtest-20260306

# Evaluate multiple specific runs
uv run python scripts/compute_backtest_metrics.py \
  --runs backtest-20260306 run-2026-03-13 run-2026-03-27

# Evaluate by relative path (from project root)
uv run python scripts/compute_backtest_metrics.py \
  --runs artifacts/ap/run-2026-04-03

# Only look at NBR and XGB
uv run python scripts/compute_backtest_metrics.py --models nbr xgb

# Only evaluate with the historical threshold method
uv run python scripts/compute_backtest_metrics.py --method historical

# Focused: one run, one model, one method — fastest output
uv run python scripts/compute_backtest_metrics.py \
  --runs backtest-20260306 \
  --models nbr \
  --method historical

# Write output to a custom directory
uv run python scripts/compute_backtest_metrics.py --output /tmp/my-eval
```

---

### What the HTML report shows

Open `metrics_output/metrics_report.html` in any browser. It has three sections:

**Model Performance Heatmap**
A color-coded grid — runs as rows, models as columns. Green = better performance,
red = worse. Switch between MAE, RMSE, Zone Accuracy, and Bias using the
metric dropdown at the top.

**Accuracy over forecast horizon**
Line charts showing how accuracy degrades week-by-week as forecasts go further
into the future. One chart per model, one line per run. Use the threshold method
radio to switch between historical and previousNweeks.

**District-level breakdown**
A sortable table showing per-district accuracy for any run + model combination.
Click column headers to sort. Color-coded by MAE.

---

### Metrics explained

| Metric | Interpretation |
|---|---|
| **MAE** (Mean Absolute Error) | Average absolute difference between predicted and actual cases. Lower is better. |
| **RMSE** (Root Mean Squared Error) | Like MAE but penalises large errors more. Lower is better. |
| **Zone Accuracy** | % of district-weeks where the predicted WHO risk zone matched the actual zone. Higher is better. |
| **Bias** | Average signed error (prediction − actual). Positive = over-predicting; negative = under-predicting. Closer to 0 is better. |

---

### Adding a new metric

Open `scripts/compute_backtest_metrics.py` and find `compute_metrics_for_group()`.
The function receives a DataFrame with columns `prediction`, `actual_cases`,
`predicted_zone`, `actual_zone`. Add your metric to the returned dict:

```python
def compute_metrics_for_group(group: pd.DataFrame) -> dict:
    ...
    # Add your metric here, e.g. Mean Absolute Percentage Error
    mape = (errors.abs() / (group["actual_cases"] + 1)).mean()

    return {
        "n": n,
        "mae": mae,
        "rmse": rmse,
        "zone_accuracy": zone_acc,
        "bias": bias,
        "mape": round(mape, 4),   # ← your new metric
    }
```

It will automatically appear in the CSV outputs. To surface it in the HTML report,
add it to the metric dropdown in `build_html_report()`.

---

## Further reading

- [RUNNING_PIPELINES.md](./RUNNING_PIPELINES.md) — scheduling, prep pipeline, full run command reference
- [CONFIG_REFERENCE.md](./CONFIG_REFERENCE.md) — every config key documented
- [MODELS.md](./MODELS.md) — NBR, RF, XGB, TSE model details
- [thresholds-explained.md](./thresholds-explained.md) — WHO and ICMR zone classification
