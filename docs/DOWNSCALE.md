# Dengue Downscale Visualization

Interactive two-map HTML showing district-level risk zones alongside
mandal-level case concentration, with a click-through interpretability panel
for every district and mandal — including a threshold gauge showing exactly
why a district received its risk classification.

---

## Folder contents

```
downscale_viz/
├── generate_viz.py   — builds the HTML from pipeline artifacts
├── template.html     — HTML/JS template (data injected at generation time)
├── README.md         — this file
└── output/           — generated HTML files land here (git-ignored)
```

Running `generate_viz.py` produces a **single self-contained HTML file**
in `output/`. No server needed — open it directly in any browser.

---

## Prerequisites

- `uv sync --all-extras` run at the repo root
- `shapely` available (included in the project extras)
- District GeoJSONs at `ap_datasets/geojsons/geojsons_AP/districts/`
- Mandal GeoJSONs at `ap_datasets/geojsons/geojsons_AP/mandals/`
- Raw IHIP case files at `ap_datasets/raw_case/`

All commands below are run from the **repo root**.

---

## Step 1 — District-level data prep

Parses IHIP case files and downloads weather data per district.
Writes output to `prepared_data/district/`.

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml \
  --run-id prep-district
```

---

## Step 2 — Mandal-level data prep

Same prep but at mandal resolution.
The downscale pipeline needs `prepared_data/mandal/cases_daily.csv`
to compute each mandal's share of recent case history.

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_mandal_prep.yaml \
  --run-id prep-mandal
```

---

## Step 3 — Main dengue forecast pipeline (district level)

Trains models, generates predictions, and computes zone classifications.
Set `run_date` to the Monday of the forecast week.

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district_v3.yaml \
  --run-id march-10-run \
  --set run.run_date=2026-03-10
```

Produces under `artifacts/ap/march-10-run/`:
- `results/Predictions_*.csv` — district predictions with zone classification
- `datasets/thresholds/district_all_thresholds.csv` — Mean and StdDev per district/week (used for the threshold gauge in the viz)

---

## Step 4 — Downscale pipeline (district → mandal)

Disaggregates district predictions to mandal level using each mandal's
proportional share of recent case history.

First, set `source_run_id` in `configs/ap_district_to_mandal.yaml` to the
run ID used in Step 3 (`march-10-run`), then run:

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/ap_district_to_mandal.yaml \
  --run-id downscale-march-10
```

Produces `artifacts/ap/downscale-march-10/results/Predictions_downscaled_*.csv`.

---

## Step 5 — Generate the visualization

```bash
uv run python diagnostic_report/downscale_viz/generate_viz.py \
  --district-run-id march-10-run \
  --mandal-run-id   downscale-march-10
```

Output:
```
diagnostic_report/downscale_viz/output/dengue_downscale_viz_march-10-run.html
```

Open this file in any browser. It is fully self-contained — GeoJSONs,
predictions, and threshold values are all embedded as inline JavaScript.

### All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--district-run-id` | `march-10-run` | Run ID from Step 3 |
| `--mandal-run-id` | `downscale-march-10` | Run ID from Step 4 |
| `--artifacts-dir` | `artifacts/ap` | Base path for all run artifacts |
| `--geojson-dir` | `ap_datasets/geojsons/geojsons_AP` | GeoJSON base directory |
| `--out` | *(auto)* | Override the output HTML path |
| `--dist-simplify` | `0.01` | Shapely tolerance for district GeoJSON simplification |
| `--mandal-simplify` | `0.005` | Shapely tolerance for mandal GeoJSON simplification |

---

## What the visualization shows

**Left map — District risk zone**
Each district is coloured green / yellow / orange / red (Low → Very High)
as classified by the dengue pipeline. Click a district to open the
interpretability panel.

**Right map — Mandal case burden**
White-to-dark-blue gradient showing predicted cases/week per mandal.
When a district is selected the scale switches to local (that district's
max), so within-district variation is always visible even when overall
numbers are low. Click any mandal for details.

**Interpretability panel (right side)**
Opens on click. For a district:
- Risk zone badge + plain-English explanation
- Predicted cases/week, AP rank, % of state total
- **Threshold gauge** — a colour-banded bar showing exactly where
  the prediction sits relative to Mean (μ), μ+σ, and μ+2σ thresholds
- Recommended action by zone level
- Which threshold method was used and what it means
- Top 4 mandals by predicted burden with a mini bar chart

For a mandal:
- Inherited zone from parent district (with explanation)
- Predicted cases/week, rank within district, % of district total
- How the downscaling was computed (proportional case share, past 4 weeks)
- Why mandals do not have independent zone classifications yet
