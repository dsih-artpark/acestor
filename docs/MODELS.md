# Models

Reference for every forecasting model registered under `pipelines/dengue/lib/models/`. Each entry covers what the model consumes, how it emits predictions, whether it can be tuned, and its known failure modes.

The runtime wiring lives in `pipelines/dengue/steps/train_and_predict.py`. That step builds one `ModelContext` per model listed in `model.models:`, calls `model.predict(ctx)`, then classifies zones and writes the result to `outputs/predictions.csv` (or per-model files under `outputs/per_model/`).

---

## Registry and short-name → full-name mapping

Models register themselves via `@register("<short>")` in `pipelines/dengue/lib/models/__init__.py`. The short name is what you put in YAML (`model.models: [nbr, rf, xgb, tse, timesfm]`). Predictions land in `predictions.csv` with the model's **full string** in the `model` column, mapped by `pipelines/dengue/lib/maps.py::MODEL_FULL_NAME`:

| Short (`model.models`, `report.primary`) | `model` column value              |
| ---------------------------------------- | --------------------------------- |
| `nbr`                                    | `negativeBinomialRegression`      |
| `rf`                                     | `randomForestRegression`          |
| `xgb`                                    | `xgboostRegression`               |
| `tse`                                    | `timeSeriesExtrapolation`         |
| `timesfm`                                | `timesFoundationModel`            |
| `ensemble`                               | `ensembleModel`                   |

---

## Common CSV schema

Every model contributes rows with the same shape after `train_and_predict` classifies them into zones. The canonical `outputs/predictions.csv` header:

```
dateOfComputingPrediction,startDatePredictedWeek,regionID,LGD_code,
prediction,predictionRaw,predictionMin,predictionMax,
thresholdMethod,predictionZone,whoZone,icmrZone,percentileZone,
Mean,StdDev,model,ISOWeek
```

Two boundary details worth remembering:

- `prediction` in the CSV is the **display integer** (`round_half_up` of the raw float — issue #83). The raw float that all internal math uses is preserved as `predictionRaw`. The downscale pipeline reverses the rename on read so nothing downstream loses precision.
- `predictionMin` / `predictionMax` are the **extremes across ensemble members** for the same group (issue #84). They are NOT confidence intervals, standard deviations, or bootstrap bounds. For per-model files, `min == max == prediction`.

A minimal sample row (KA district, ensemble, previousNweeks method):

```
2025-11-24,2025-12-01,district_KA_29_08,29-08,4,4.37,3,6,previousNweeks,3,3,2,3,3.10,1.44,ensembleModel,49
```

---

## `nbr` — Negative Binomial Regression

**Intent.** GLM with a negative-binomial family, one-hot on ISO week, fitted on all history except the last four weeks and applied to those four. Ported straight from the GBA pipeline.

**Features consumed.** Yes — weather. Lag-shifted `t2m_mean` (temperature), `tp_sum` (rainfall), `d2m_mean` (humidity) plus ISO-week one-hots. **No case lags.** `model.data_features` is passed through as the non-lag whitelist.

**Horizon handling.** Fits on all rows with `recordDate <= pred_upto` except the last four; predicts those four in one shot. No recursion, no persistence — every forecast week has its own weather row available at prediction time.

**Prediction location.** Rows land in the ensemble file at `outputs/predictions.csv` and (if `output: per_model | both`) at `outputs/per_model/predictions_nbr.csv` with `model = "negativeBinomialRegression"`.

**Tuneable?** No. NBR has no Optuna path — `alpha=1.0` is fixed. `model.tune` only affects `rf` and `xgb`.

**Known failure modes.**

- Regions with any NaN lag feature across all four prediction weeks are dropped with a warning (weather coverage gaps). They render white/hatched on maps.
- If every region has NaN lags, `predict()` returns an empty DataFrame and `train_and_predict` raises `RuntimeError` for the whole run (issue #65 — partial dropout no longer silently succeeds).
- `MinMaxScaler` will crash on a region where all lag features are NaN; the pre-drop above prevents this.

---

## `tse` — Time-Series Extrapolation

**Intent.** Two-week linear extrapolation of a 4-week moving average per region. The simplest baseline in the ensemble; almost no assumptions.

**Features consumed.** Cases only. Ignores weather entirely. Reads `case_df` directly (not the merged frame).

**Horizon handling.** Special — TSE only goes **2 weeks past `cutoff_case`**, not 4. `TSEModel.predict()` sets `tse_upto = ctx.cutoff_case + 14 days` and stops there. When the ensemble asks for four weeks, TSE contributes rows only for weeks 1 and 2.

**Prediction location.** Rows land with `model = "timeSeriesExtrapolation"` in the same files as above.

**Tuneable?** No.

**Known failure modes.**

- Regions with fewer than 2 observations, or whose most recent observation is >15 days before `to_date`, are skipped and logged as WARNING.
- Values are floored at 0. There is no upper clip.

---

## `rf` — Random Forest Regression

**Intent.** `RandomForestRegressor` trained on all-but-last-4 weeks, applied **recursively** across the 4-week horizon.

**Features consumed.** Weather (temp / rainfall / humidity lags) AND case lags (`lag_cases`). ISO-week one-hots.

**Horizon handling.** Recursive multi-step via `pipelines/dengue/lib/models/_shared.recursive_forecast`:

- **Weather / exogenous lags** for future weeks are read from `df0` and, by default, **frozen at the forecast origin** (`freeze_weather_at_origin=True` — persistence assumption, parity with vbd-modelbench `predict_rt.py`, issue #77). Setting it to `False` advances per week using each future row instead — this leaks observed future weather and is only defensible in hindcasts. Freezing enables shorter case-lag windows to be used safely.
- **Case lags** are filled hybrid-Y: a lag pointing at a future week uses that week's own prediction (already written back into `case_by_date`); a lag pointing at an observed week uses the observed value; a lag pointing before the series start is zero-padded. Each week's prediction is fed forward.

Predictions are floored at 0. If `clip_multiplier` is set, they are additionally capped at `clip_multiplier × train_max` and the row records `was_clipped=True`.

**Prediction location.** `model = "randomForestRegression"`.

**Tuneable?** Yes. See [Tuning](#tuning--optuna-three-modes) below. Cache location: `hp/rf_<region_type>_best_params.json` and `hp/rf_<region_type>_fingerprint.json`.

**Known failure modes.**

- If the origin row has any NaN weather feature, the region is skipped (persistence is undefined). If ALL regions skip, `predict()` returns empty and the run fails.
- Training frame empty after `years_to_include` / `years_to_exclude` filtering → empty predictions.

---

## `xgb` — XGBoost Regression

**Intent.** Same shape as `rf` but with `XGBRegressor`. Trained on all-but-last-4, applied recursively.

**Features consumed.** Identical to `rf` — weather lags, case lags, ISO week.

**Horizon handling.** Same `recursive_forecast` engine, same `freeze_weather_at_origin` behaviour, same hybrid-Y case-lag fill.

**Prediction location.** `model = "xgboostRegression"`.

**Tuneable?** Yes. Cache: `hp/xgb_<region_type>_best_params.json` and matching `_fingerprint.json`. Defaults are the vbd-modelbench values: `n_estimators=300, learning_rate=0.05, max_depth=5, min_child_weight=5, subsample=0.8, colsample_bytree=0.8, gamma=0.1, reg_alpha=0.1, reg_lambda=5.0`.

**Known failure modes.** Same as RF. Additionally, xgboost carries its own OpenMP runtime — do NOT try to import torch in the same worker; that's the whole reason TimesFM runs in a subprocess (below).

---

## `timesfm` — TimesFM 2.5-200m Foundation Model

**Intent.** Google's pretrained univariate foundation model applied to each region's weekly case series. **No weather; no per-region training; case history only.**

**Isolation.** TimesFM is imported and run in a **fresh subprocess** via `pipelines/dengue/lib/isolation.py::run_isolated` (issue #46). The model registry already imports xgboost at module load, and torch loaded in the same process as xgboost's OpenMP runtime segfaults. The isolation contract is file-based:

- Parent writes `series.npy`, `lengths.npy`, `spec.json` to a temp dir.
- Parent spawns `python -m pipelines.dengue.lib._timesfm_worker <workdir>` with `start_new_session=True` so the whole process group can be signalled on timeout.
- Child writes `output.npy` and `status.json`.
- Every failure mode (timeout, non-zero exit, missing / unparseable status, wrong shape, non-finite values) is translated into `IsolatedRunError` with a useful message.

**Checkpoint warm.** The worker pulls the HF revision on first run and caches it under `.cache/timesfm/` (project-local, gitignored). `TIMESFM_REVISION` env var overrides the pinned SHA `1d952420fba87f3c6dee4f240de0f1a0fbc790e3` without a code change — useful for ops to roll checkpoints.

**Config.** All keys under `model_configs.timesfm.*` are **optional** — sensible defaults are baked in. The minimum config is:

```yaml
model:
  models: [timesfm]
  spatial_res: district
  ensemble: none
  output: per_model
# model_configs.timesfm can be omitted entirely; defaults apply.
```

Defaults:

```yaml
model_configs:
  timesfm:
    huggingface_repo_id: google/timesfm-2.5-200m-pytorch
    revision:                    # env TIMESFM_REVISION, else pinned 40-hex SHA
    cache_dir: .cache/timesfm
    max_context: 1024
    per_core_batch_size: 32
    min_context_weeks: 52
    timeout_s: 300
    max_regions_per_batch: 128
```

`revision` MUST be a full 40-hex commit SHA. Tags and branches are rejected — a HF tag can move under you and reproducibility is gone.

**`min_context_weeks` policy.** A region needs at least this many weekly observations (default 52). Below the floor → skipped with a warning. All-zero series ≥ the floor are emitted as zero forecasts (no need to consult the model). If EVERY region is below the floor, `predict()` raises with a hint to lower `min_context_weeks` or pick a coarser `spatial_res` — high-res levels like ward or mandal often need this.

**Features consumed.** Cases only. Reads `case_df`, not `merged_df`.

**Horizon handling.** Weekly grid anchored at `cutoff_case`. `horizon_and_indices_for_targets` computes `(d - cutoff_case).days // 7 - 1` per target date; off-grid or non-positive offsets are a hard error. Every region shares one horizon (the max across targets).

**Prediction location.** `model = "timesFoundationModel"`.

**Tuneable?** No. There is nothing to tune — it's a frozen pretrained checkpoint.

**Ensemble status.** Standalone, backtest-gated. It is NOT wired into the default ensemble; use it via `model.models: [timesfm]` with `ensemble: none, output: per_model` until the validation gate lands.

**Known failure modes.**

- Off-weekly-cadence input in a region (a date that isn't `anchor + k*7 days`) raises `ValueError` before any inference — a data-integrity signal, not something to silently interpolate.
- Worker timeout at 300s by default — bump `timeout_s` for large batches or slow disks (first-run HF pull).
- Non-finite outputs bubble up as `IsolatedRunError`.

---

## `freeze_weather_at_origin` — persistence for future weather

Set on `TrainPredictConfig`; default `False` on the config field (see #77 for context), but `recursive_forecast` treats `True` as its own internal default when called without a context. In practice pipelines pass the value explicitly. Affects `rf` and `xgb` (via the shared `recursive_forecast` engine). Two modes:

- **`True` (persistence).** Weather / iso lag columns for every future week are copied from the forecast origin's row. Case lags are still filled hybrid-Y. This matches upstream vbd-modelbench and enables case-lag windows shorter than the horizon (a `case_lag_1` on week +3 references week +2's own prediction, not an observed value).
- **`False` (per-week advance).** Each future week reads its own row from `df0`. Requires `lag_cases[i] >= horizon` so the referenced case week is observed. Any NaN in any future week's non-case features → region skipped. This mode leaks observed future weather and is only useful in strict hindcasts where the future weather is known.

---

## Tuning — Optuna, three modes

`rf` and `xgb` support hyperparameter tuning driven by `pipelines/dengue/lib/models/_tuning.py`. The search spaces mirror vbd-modelbench (`tune_rf_cv` / `tune_xgb_cv`). Expanding-window CV over the training weeks; RMSE minimized.

Three modes on `model.tune`:

| Value     | Behaviour                                                                                                                     |
| --------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `true`    | **Always tune.** Ignore the cache, run Optuna, write new params + fingerprint.                                                |
| `false`   | **Cache-then-tune (default).** Load cached params; if the fingerprint matches the current config + training cutoff, use them. Otherwise auto-retune. |
| `"never"` | **Cache-only.** Trust the cache regardless of fingerprint (hindcast / production reuse). If the cache is missing, **raise `FileNotFoundError`** — fail loud rather than silently retune. |

The `"never"` mode was added specifically so hindcasts and scheduled production runs can reuse a one-time tuning result across many vintages without either (a) re-tuning on every vintage or (b) silently drifting when the fingerprint invalidates.

**Fingerprint contents.** SHA-256 of a JSON payload of: `lag_temp`, `lag_rainfall`, `lag_humidity`, `lag_cases`, `data_features`, `years_to_include`, `years_to_exclude`, `train_max_date`, and `region_type` (so a district cache never leaks into a ward run).

**Cache paths.**

```
hp/rf_<region_type>_best_params.json
hp/rf_<region_type>_fingerprint.json
hp/xgb_<region_type>_best_params.json
hp/xgb_<region_type>_fingerprint.json
```

`_best_params.json` payload:

```json
{
  "tuned_at": "2025-11-14",
  "n_trials": 100,
  "best_rmse": 12.4381,
  "params": { "n_estimators": 550, "max_depth": 24, "min_samples_leaf": 3, "max_features": "sqrt" }
}
```

Priming the cache for `tune="never"`:

```bash
# One-time: tune at every spatial level you plan to run production against.
uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district.yaml \
  --run-id prime-hp-district \
  --override model.tune=true --override model.n_trials=100

# Then all downstream vintages:
uv run python -m acestor.run --config configs/ap_district.yaml \
  --run-id hindcast-2024-w01 --override model.tune=never
```

---

## Ensembling

Configured under `model:`:

```yaml
model:
  models: [nbr, rf, xgb, tse]
  ensemble: mean          # or "none"
  output: ensemble        # or "per_model" | "both"

report:
  primary: ensemble       # controls which model drives the brief; short name
```

- `ensemble: mean` — `pipelines/dengue/lib/ensembles.py::MeanEnsemble.combine` averages `prediction` across models on the shared key (region + week + thresholdMethod), rewrites `model = "ensembleModel"`, and recomputes ISOWeek from `recordDate`. `predictionMin` / `predictionMax` are then attached by `_add_prediction_range` — min and max of the members within each group (issue #84).
- `ensemble: none` — no combining. Only valid with `output: per_model`.

**Output modes** (resolved by `_resolve_predictions_paths`):

| `output`     | Canonical CSV                       | Extra files                                       |
| ------------ | ----------------------------------- | ------------------------------------------------- |
| `ensemble`   | `outputs/predictions.csv` (ensemble) | —                                                 |
| `both`       | `outputs/predictions.csv` (ensemble) | `outputs/per_model/predictions_<m>.csv` per model |
| `per_model`  | `outputs/predictions.csv` (copy of primary) | `outputs/per_model/predictions_<m>.csv` per model |

In `per_model` mode there is always one canonical `outputs/predictions.csv` at the root — a copy of `report.primary`'s per-model file — so officials and the HTML brief have a single source of truth.

---

## Threshold anchoring — `threshold_to_date` is `cutoff_case`

Every model returns `ctx.cutoff_case` from `threshold_to_date()` (issue #101). Previously `nbr`, `rf`, `xgb` used `pred_upto - 28 days`, which was byte-identical to `cutoff_case` only when the horizon was exactly 28 days and silently diverged otherwise (weekday-anchored sampling can produce a 21-day horizon). The unified `cutoff_case` anchor makes per-model outputs group-consistent and unblocks ensembling across mixed horizons.
