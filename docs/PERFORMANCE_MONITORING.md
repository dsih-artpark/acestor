# dengue_monitoring Pipeline

`dengue_monitoring` calculates forecast errors from acestor predictions and observed case counts.
It compares current errors with thresholds calculated from a fixed historical period.

Entry point: `pipelines.dengue_monitoring.pipeline:build_pipeline`.

See [DENGUE_PREP.md](DENGUE_PREP.md) for case preparation and [SOURCES.md](SOURCES.md) for the dashboard case source.

---

## What it does

The pipeline has two modes:

| Mode | Behaviour |
|---|---|
| `build_baseline` | Calculates historical errors and statistics, saves a baseline version, and marks it as active. |
| `monitor` (default) | Loads the baseline, reads or fetches current actuals, calculates current errors, and writes flags, alerts and an HTML report. |

Each scope identifies a geography, spatial scale and forecast derivation.
Separate scopes keep native, rolled-up and downscaled forecasts separate.
Each scope, model and lead has its own historical error distribution.
Regions are pooled within that distribution; flags and alert sequences are calculated separately for each region.


---

## Input CSV schemas

### Prediction CSV

A consolidated CSV requires these columns:

| Column | Meaning |
|---|---|
| `regionID` | Region identifier; retain leading zeros. |
| `model` | Acestor model label or configuration name, such as `randomForestRegression` or `rf`. |
| `startDatePredictedWeek` | Native acestor label for the **end** of a trailing seven-day total. |
| `predictionRaw` | Unrounded predicted case count. |
| `issue_date` | Logical forecast issue date. |
| `lead` | Original forecast lead; do not renumber after filtering dates. |

The pipeline calculates errors itself. Identical repeated predictions are deduplicated; conflicting duplicates fail.
Finite negative predictions remain unchanged and receive a `negative_prediction` marker.
Use `target_label: start` only when the supplied forecast labels actually denote week starts.

### Actuals CSV

Put an `actuals` mapping under both `historical` and `current`.

| Frequency | Default columns | How scoring works |
|---|---|---|
| `daily` | `region_id`, `date`, `case_count` | Sums the exact seven-day target window. |
| `weekly` | `region_id`, `target_start`, `target_end`, `actual_cases` | Joins on region and both window dates; no further aggregation. |

For a supplied file, set `refresh_actuals: false` and provide `actuals.path`.
Dates must be calendar dates without times or timezones. Actual counts must be non-negative.
Weekly windows must span seven inclusive days without overlaps or duplicate rows.

For acestor's sampled weekly case files, use this mapping inside the selected period:

```yaml
refresh_actuals: false
actuals:
  frequency: weekly
  path: /path/to/cases_zone_sampled.csv
  region_column: location.admin3.ID
  value_column: case
  date_column: metadata.primaryDate
  week_label: end
```

Other column mappings use `region_column`, `value_column`, `date_column`, or weekly `start_column` and `end_column`.
Weekly actuals must match the forecast calendar: Tuesday totals cannot score Sunday targets.

### Case data refresh

`current.refresh_actuals` defaults to `true`; `historical.refresh_actuals` defaults to `false`.
To fetch current actuals, configure `current.prep_config` (or scope-level `prep_config`) and use daily actuals without an explicit path.
The prep configuration selects the source, including the existing dashboard connector.

Monitoring runs the existing geojson download, case download and case parser.
It reads `<data.prepared_data.base_dir>/<spatial_scale>/cases_daily.csv` and verifies that preparation rewrote the requested scale's file.
Disabled case downloading or unchanged output fails the run.
Supplied daily/weekly files must be refreshed by their producer.

**Data limitation:** sparse daily data treats absent days inside the file's global date extent as zero for known regions.
Outside that extent, actuals are unavailable. Missing weekly totals remain missing.
A successful refresh does not establish reporting completeness; actuals maturity remains `unverified`.

---

## Configuration

Settings live under `monitoring:`. Scope settings belong to entries in `monitoring.scopes`.

| Setting | Description |
|---|---|
| `monitoring.as_of` | Evaluation cutoff date. Later target windows remain `pending`. Update this as data arrives. |
| `monitoring.baseline.directory` / `name` | Local baseline registry and the name of the active reference. |
| `monitoring.baseline.id` | Optional full SHA-256 to pin a specific baseline instead of following the active name. |
| Scope `name`, `spatial_scale`, `derivation` | Scope identifier, spatial scale and derivation (`native`, `rollup` or `downscale`). |
| Scope `models`, `leads` | Models available in both periods and positive lead numbers, usually `[1, 2, 3, 4]`. |
| Scope `region_ids` | Optional explicit cohort. Otherwise, baseline construction uses the IDs in historical actuals. |
| `historical.start` / `end` | Dates used to calculate the historical error distribution. |
| `current.start` / `end` | Dates to evaluate, after the historical period. Include preceding weeks to calculate consecutive-week alerts. |
| Each period's `predictions_file` | Consolidated forecasts. Both periods may point to the same file. |
| Each period's `actuals` / `refresh_actuals` | Independent historical and current actuals settings; see below. |

**Period dates select target-window end dates inclusively, not forecast issue dates.**
A January target issued in December belongs to January's evaluation.
Keep spatial scales separate; supply the corresponding acestor outputs and actuals for each scope.

---

## Error calculation and alerts

```text
signed_error = (predictionRaw - actual_cases) / actual_cases
```

| Condition | Result |
|---|---|
| Error strictly above historical p75 | Upper flag; equality does not count. |
| Two or more consecutive target weeks above p75 | Alert on the second and subsequent qualifying weeks, separately per region/model/lead. |
| Error below zero | Underprediction flag only. |
| Missing prediction, missing actual, zero actual or future target | Unscored row with an explicit status. |
| Unscored or absent calendar week | Breaks the alert sequence. |

A negative p75 can produce both flags. Underprediction alone does not trigger an alert.
Sequences use the current observations supplied in that run; they do not accumulate across executions.

Historical statistics pool valid region-week errors across selected years.
Percentiles use nearest rank; standard deviation uses `ddof=1`.
A reference with no valid errors cannot produce upper-threshold alerts.

---

## Baseline versions

Routine runs verify and reuse the saved baseline. They never reopen historical source files or recalculate historical statistics or thresholds.
Current actuals revisions affect only the new evaluation.

The `build_baseline` mode saves a version under `<baseline.directory>/<SHA-256>/`.
It activates that version only after required calibration artifacts are written; earlier versions remain available.
A pinned `baseline.id` continues selecting its version until you remove or change the pin.

Rebuild the baseline when changing spatial scales, models, leads, region sets, date conventions or historical dates.
Forecasts containing new regions fail unless `region_ids` explicitly selects the existing region set.
Rebuild the baseline to include new regions.

---

## CLI recipes

Run these commands from the repository root.
The example configuration is [monitoring.gba_archives.yaml](../configs/monitoring.gba_archives.yaml).

| Example setting | Value |
|---|---|
| Historical target dates | 1 January 2024–31 December 2025 |
| Current target dates | 1 January–27 September 2026 |
| Actuals | Weekly totals from local CSV files |
| Case download | Disabled (`refresh_actuals: false`) |

### Build the historical baseline

Run once before evaluating current forecasts.
Run again only to replace the historical error distribution and its thresholds.

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_monitoring.pipeline:build_pipeline \
  --config configs/monitoring.gba_archives.yaml \
  --set monitoring.mode=build_baseline
```

### Evaluate current forecasts

Update the prediction and actuals files, `current.end`, and `monitoring.as_of` before running:

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_monitoring.pipeline:build_pipeline \
  --config configs/monitoring.gba_archives.yaml
```

The CLI generates a unique run ID when `--run-id` is omitted.
Reused IDs and `--clean` are rejected. Use a new ID after a failed run.
Open `<artifacts base_path>/<run_id>/outputs/report.html` to view the results.

---

## Output artifacts

Paths below are relative to the monitoring run directory.

| Artifact | Contents |
|---|---|
| `outputs/report.html` | Filterable charts, reference statistics and current coverage. |
| `outputs/current_errors.csv` | Predictions, actuals, errors, statuses, flags and run identity. |
| `outputs/flags.csv`, `alerts.csv` | Flagged observations and consecutive-week alerts. |
| `outputs/exclusions.csv` | **Current-period** unscored observations only. |
| `outputs/baseline.csv` | Frozen percentiles, mean, standard deviation and sample counts. |
| `outputs/manifest.json` | Input hashes, rules, baseline location, execution ID and run/evaluation hashes. |
| `inputs/<scope>/current/cases_daily.csv` or `cases_weekly.csv` | Actuals snapshot used by this evaluation. |

Historical observations and exclusions stay in the baseline bundle's `historical_errors.csv`.
Use the manifest's `baseline_directory` to locate them. Baseline-build runs also export historical errors.
Include the bundle when sharing a routine run if the recipient needs historical observations.

The HTML report filters charts by scope, region, model and lead. It works offline with JavaScript enabled.

---

## Troubleshooting

| Failure | Action |
|---|---|
| No frozen baseline | Run `build_baseline` once with historical inputs. |
| Settings differ from the baseline | Restore matching settings or explicitly build a compatible reference. |
| Regions outside the cohort | Correct the inputs, explicitly select the existing cohort, or rebuild to include new regions. |
| Refresh disabled or output not rewritten | Check the prep source, enabled flag, region types and output directory. |
| Weekly alignment mismatch | Supply totals for the exact forecast windows; do not shift weekly labels. |
| Run ID already used | Omit `--run-id` to generate a new one, including after a failed claimed run. |
| Baseline integrity/version error | Restore an intact supported bundle or rebuild; do not edit frozen files. |

---

## Forecast archives and historical reruns

Instead of `predictions_file`, use `historical.runs` or scope-level `current_runs` with `issue_date` and `run_dir`.
Archive loading reads `outputs/predictions.csv` and per-model files, using ordered `prediction_dates` from `cutoffs.json` to assign leads.
Use `metadata_dir` for parent metadata, `prediction_dates` for explicit dates, or `prediction_files` for custom file/model mappings.

For generated history, supply `historical.issue_start` / `issue_end` and `forecast_config`.
Origins advance every seven days. Include earlier origins needed to cover the first target windows.
`reuse_existing_runs: true` reuses archives or cached backtests; `false` regenerates into new directories.
Weather and other forecasting inputs must already exist.

Derived reruns require the matching `forecast_builder` and an explicit parent `run.source_run_id`, supplied through run-spec overrides when needed.
`latest` is rejected; missing parent forecasts must be produced separately.
Reruns use currently available historical data, not reconstructed data vintages.

---