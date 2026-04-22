# Running the Pipelines

There are two separate pipelines in this repo that work together to produce a dengue forecast:

| Pipeline | Module | Purpose |
|---|---|---|
| **dengue_prep** | `pipelines.dengue_prep.pipeline` | Downloads and prepares raw case and weather data into `prepared_data/` |
| **dengue** | `pipelines.dengue.pipeline` | Reads from `prepared_data/`, runs the forecast model, generates maps and reports |

They are **independent processes** — `dengue_prep` writes files to disk that `dengue` later reads. You can run them back-to-back or on separate schedules (e.g., prep daily, forecast weekly).

---

## Running manually (one-shot)

Use `python -m acestor.run` from the repo root:

```bash
# Step 1 — prepare data
python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml

# Step 2 — run forecast
python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district.yaml
```

The two steps can be chained in a shell script:

```bash
#!/bin/bash
set -e
python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml

python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district.yaml
```

> **Note:** `dengue_prep` is designed to be idempotent — re-running it upserts new data without discarding what was already prepared. You can run it as often as you need fresh data before triggering a forecast.

---

## Scheduling with `run_schedules.py`

`scripts/run_schedules.py` is an [APScheduler](https://apscheduler.readthedocs.io/)-based scheduler. Add one entry per pipeline to the `PIPELINES` list at the top of the file:

```python
# scripts/run_schedules.py
PIPELINES = [
    {
        "name":     "ap-prep-daily",
        "cron":     "0 1 * * *",       # every day at 01:00 UTC (06:30 IST)
        "pipeline": "pipelines.dengue_prep.pipeline:build_pipeline",
        "config":   "configs/ap_district_prep.yaml",
    },
    {
        "name":     "ap-forecast-weekly",
        "cron":     "30 2 * * 2",      # every Tuesday at 02:30 UTC (08:00 IST)
        "pipeline": "pipelines.dengue.pipeline:build_pipeline",
        "config":   "configs/ap_district.yaml",
    },
]
```

Then start the scheduler:

```bash
# Foreground (for testing)
python scripts/run_schedules.py

# Background — Linux/macOS
nohup python scripts/run_schedules.py > .acestor/scheduler.out 2>&1 &

# Background — Windows
start /B pythonw scripts/run_schedules.py
```

Each run writes its own log file to `logs/<name>/<run-id>.log`.

> **Scheduling tip:** Set the prep cron to run at least an hour before the forecast cron so that `prepared_data/` is fully refreshed before the forecast reads it.

---

## Scheduling via crontab (`install_schedule.py`)

As an alternative to the APScheduler process, you can install a raw crontab entry using `scripts/install_schedule.py`. This requires the config YAML to include a `schedule` section:

```yaml
# configs/ap_district.yaml
schedule:
  cron: "30 2 * * 2"
  pipeline: "pipelines.dengue.pipeline:build_pipeline"
  config: "configs/ap_district.yaml"
```

Then install it:

```bash
python scripts/install_schedule.py configs/ap_district.yaml
```

This replaces the current user's crontab with a single entry. For most deployments, the `run_schedules.py` approach is preferred because it supports multiple pipelines in one process and writes structured logs.

---

## Config relationship between the two pipelines

`dengue_prep` writes its output to a directory configured by `data.prepared_data.base_dir` (default: `prepared_data`). The `dengue` pipeline reads from the same path:

```yaml
# configs/ap_district_prep.yaml  (dengue_prep)
data:
  prepared_data:
    base_dir: "prepared_data"
    region_type: "district"

# configs/ap_district.yaml  (dengue)
data:
  prepared_data:
    base_dir: "prepared_data"      # ← must match the prep config
    region_type: "district"        # ← must match the prep config
```

If you change `base_dir` or `region_type` in the prep config, update the forecast config to match.

---

## Checking run artifacts

Each run writes artifacts under `storages.artifacts.filesystem.base_path` (default: `./artifacts`), organised by `run_id`:

```
artifacts/
  <run_id>/          ← one folder per run
    cutoffs.json
    datasets/
    results/
    plots/
    reports/
```

Logs are written separately to `logs/<scheduler-name>/<run_id>.log` when using the scheduler, or printed to stdout for manual runs.
