# Dengue Downscale Visualization

Interactive two-map HTML showing district-level risk zones alongside
mandal-level case concentration, with a click-through interpretability panel
for every district and mandal — including a threshold gauge showing exactly
why a district received its risk classification.

---

## Files

```
scripts/downscale_viz/
├── generate_viz.py   — builds the HTML from pipeline artifacts
├── template.html     — HTML/JS template (data injected at generation time)
└── output/           — generated HTML files land here (git-ignored)

docs/downscale.md     — this guide
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

Set `run_date` in `configs/ap_district_v3.yaml` to the Monday of the forecast week, then run:

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district_v3.yaml \
  --run-id march-10-run
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

Produces:

- `artifacts/ap/downscale-march-10/outputs/predictions.csv` — child-level predictions CSV.
- `artifacts/ap/downscale-march-10/outputs/maps/*.png` — per-week static choropleth PNGs (one per `(week, model, threshold)`), rendered against the child geojson layer. Suitable for offline use and PDF embedding; produced by the `generate_downscale_maps` step.

Configure under `downscale_maps:` (all keys optional, sensible defaults):

```yaml
downscale_maps:
  enabled: true                   # set false to skip PNG output
  output_dir: "outputs/maps"      # under the run-id artifacts dir
  figure_title: "AP Mandal Risk"  # appears on each PNG
```

---

## Step 5 — Generate the visualization

```bash
uv run python scripts/downscale_viz/generate_viz.py \
  --parent-run-id march-10-run \
  --child-run-id  downscale-march-10
```

Output:
```
scripts/downscale_viz/output/dengue_downscale_viz_march-10-run.html
```

Open this file in any browser. It is fully self-contained — GeoJSONs,
predictions, and threshold values are all embedded as inline JavaScript.

### All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--parent-run-id` | `march-10-run` | Run ID from Step 3 (parent-level forecast) |
| `--child-run-id` | `downscale-march-10` | Run ID from Step 4 (downscale pipeline) |
| `--parent-level` | `district` | Parent geography singular (e.g. `district`, `state`) |
| `--child-level` | `mandal` | Child geography singular (e.g. `mandal`, `block`) |
| `--artifacts-dir` | `artifacts/ap` | Base path for all run artifacts |
| `--geojson-dir` | `ap_datasets/geojsons/geojsons_AP` | GeoJSON base directory |
| `--out` | *(auto)* | Override the output HTML path |
| `--parent-simplify` | `0.01` | Shapely tolerance for parent GeoJSON simplification |
| `--child-simplify` | `0.005` | Shapely tolerance for child GeoJSON simplification |

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
