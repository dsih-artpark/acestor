# Models — Configuration and Extension Guide

The dengue pipeline runs one or more prediction models via a registry. Models register themselves at import time and are selected via YAML. This doc covers how to enable/disable models, how to inspect outputs, and how to add a new model.

---

## Available models

| Name | Class | Source | What it does |
|------|-------|--------|--------------|
| `nbr` | `NBRModel` | [pipelines/dengue/lib/models/nbr.py](../pipelines/dengue/lib/models/nbr.py) | Negative Binomial Regression on case counts using lagged temperature, rainfall, and humidity. Predicts the last 4 weeks of available weather data. |
| `rf` | `RFModel` | [pipelines/dengue/lib/models/rf.py](../pipelines/dengue/lib/models/rf.py) | Random Forest Regression using lagged temperature, rainfall, and humidity. Predicts the last 4 weeks of available weather data. Default: 200 trees, max_depth=10. |
| `tse` | `TSEModel` | [pipelines/dengue/lib/models/tse.py](../pipelines/dengue/lib/models/tse.py) | Time-Series Extrapolation — linear trend on a 4-week moving average. Predicts 2 weeks ahead from the case cutoff. Case-only, no weather. |
| `xgb` | `XGBModel` | [pipelines/dengue/lib/models/xgb.py](../pipelines/dengue/lib/models/xgb.py) | XGBoost Regression using lagged temperature, rainfall, and humidity. Predicts the last 4 weeks of available weather data. Default: 300 estimators, lr=0.05, max_depth=5. |

Inspect the live registry from a Python shell:

```bash
uv run python -c "from pipelines.dengue.lib.models import _REGISTRY; print(sorted(_REGISTRY))"
# → ['nbr', 'rf', 'tse', 'xgb']
```

---

## Configuring which models run

Set `model.models` in the pipeline YAML. The default (when omitted) is `[nbr, tse]`.

```yaml
model:
  spatial_res: "district"
  models: [nbr, tse]    # which models to run
  ensemble: mean        # how to combine — registered ensemble strategy or "none"
  output: ensemble      # what to write — "ensemble" | "per_model" | "both"
  data_features: [...]
  lag:
    lag_temp: [12]
    lag_rf: [4]
  list_alpha: [2.0, 3.0]

report:
  primary: ensemble     # which CSV feeds maps + report — "ensemble" or any model key
  output_dir: reports
  compile_pdf: true
```

All three new keys (`ensemble`, `output`, `report.primary`) default to values that preserve the original pipeline behaviour, so existing configs do not need to change.

### Selecting a subset of models

```yaml
model:
  models: [nbr]         # NBR only — output labelled "negativeBinomialRegression"
```

```yaml
model:
  models: [tse]         # TSE only — output labelled "timeSeriesExtrapolation"
```

```yaml
model:
  models: [nbr, tse]    # both — output labelled "ensembleModel" by MeanEnsemble
```

### Choosing the ensemble strategy

`model.ensemble` selects how multiple model outputs are combined. The currently registered strategies:

| Name | Class | Behaviour |
|------|-------|-----------|
| `mean` | `MeanEnsemble` | Arithmetic mean of `prediction` per (region, date, ...) group. Sets `model = "ensembleModel"`. This is the default. |
| `none` | _(sentinel)_ | Skip combining entirely. Only valid with `output: per_model` or `output: both` (config validation enforces this). |

Inspect the live ensemble registry:

```bash
uv run python -c "from pipelines.dengue.lib.ensembles import _ENSEMBLE_REGISTRY; print(sorted(_ENSEMBLE_REGISTRY))"
# → ['mean']
```

### Choosing what gets written to disk

`model.output` controls which CSVs land in `artifacts/<region>/<run-id>/results/`:

| Setting | Files written |
|---------|---------------|
| `ensemble` (default) | one combined CSV — `Predictions_<MonthYear>_<RegionType>_<YYYYMMDD>.csv` |
| `per_model` | one CSV per model — `Predictions_<MonthYear>_<RegionType>_<model_key>_<YYYYMMDD>.csv` |
| `both` | per-model CSVs **and** the ensemble CSV |

### Picking which CSV maps + report consume

Maps and the report still consume a single CSV. `report.primary` selects which one:

- `report.primary: ensemble` (default) — uses the ensemble CSV. Requires `output: ensemble` or `output: both`.
- `report.primary: nbr` (or any other model key) — uses that model's per-model CSV. Requires that the model is in `cfg.models` and that `output` is `per_model` or `both`.

If `report.primary` does not match any CSV that was written, the step raises `ValueError` at runtime. The set of valid values depends on `cfg.models` and `cfg.output`, so validation has to happen at runtime (not at config load).

### Behaviour rules

- **One model configured** → the single model's name flows through to the output CSV's `model` column even when ensembling is on.
- **Two or more models configured + `ensemble: mean`** → predictions are run independently, then combined by `MeanEnsemble.combine` and labelled `ensembleModel`.
- **`ensemble: none`** → no combining. `output` must be `per_model` or `both`.
- **A model returns no predictions** (e.g. NBR has no overlapping case/weather coverage) → a warning is logged and the model is skipped; remaining models still run.
- **All models return empty** → the step returns an empty `PredictionResult` and downstream maps appear white/hatched.

---

## Where to see model outputs

All CSVs land in `artifacts/<region>/<run-id>/results/`. Filenames are driven by `model.output`:

| `output` | Files written |
|----------|---------------|
| `ensemble` | `Predictions_<MonthYear>_<RegionType>_<YYYYMMDD>.csv` (combined) |
| `per_model` | `Predictions_<MonthYear>_<RegionType>_<model_key>_<YYYYMMDD>.csv` per model |
| `both` | both of the above |

Every CSV is post-threshold-classified. The `model` column distinguishes per-model rows (`negativeBinomialRegression`, `timeSeriesExtrapolation`) from ensemble rows (`ensembleModel`).

### Example — see all three outputs

```yaml
model:
  models: [nbr, tse]
  ensemble: mean
  output: both
report:
  primary: ensemble
```

After running, the results directory contains:
```
Predictions_Apr 2026_District_20260427.csv         # ensemble (mean of nbr + tse)
Predictions_Apr 2026_District_nbr_20260427.csv     # NBR alone
Predictions__District_tse_20260427.csv             # TSE alone (empty MonthYear when no future dates)
```

> Note on the empty `MonthYear` slot: it depends on whether a model's predictions fall on/after `run_date`. TSE only predicts 2 weeks ahead from the case cutoff, so when `run_date` is later than the case cutoff + 14 days the slot collapses to empty. This is cosmetic — the file itself contains valid TSE predictions for inspection.

### Inspecting a single model from Python

```python
from pipelines.dengue.lib.models import get_model, ModelContext
ctx = ModelContext(merged_df=..., case_df=..., cfg=..., pred_upto=..., cutoff_case=...)
nbr_only = get_model("nbr").predict(ctx)
tse_only = get_model("tse").predict(ctx)
```

---

## Adding a new model

Three changes — one file, one decorator, one import.

### 1. Create the model file

`pipelines/dengue/lib/models/my_model.py`:

```python
from __future__ import annotations

import pandas as pd

from pipelines.dengue.lib.models import ModelContext, register


@register("my_model")
class MyModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        """Return a DataFrame with at least: spatial_col, recordDate, prediction, model."""
        # ctx.merged_df    → case + weather joined (use this if you need weather)
        # ctx.case_df      → case-only DataFrame (use this if you don't need weather)
        # ctx.cfg          → TrainPredictConfig (spatial_res, data_features, lag_*, etc.)
        # ctx.pred_upto    → prediction horizon (pd.Timestamp)
        # ctx.cutoff_case  → last reliable case-data date (pd.Timestamp)

        rows = []
        for region, grp in ctx.case_df.groupby(ctx.cfg.spatial_res):
            future_date = ctx.pred_upto
            rows.append({
                ctx.cfg.spatial_res: region,
                "recordDate": future_date,
                "prediction": float(grp["case"].mean()),
                "model": "myModel",
            })
        return pd.DataFrame(rows)

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        """Date passed to merge_predictions_thresholds — typically pred_upto - 28 days
        for weather-driven models or cutoff_case for case-only models."""
        return ctx.pred_upto - pd.Timedelta(days=28)
```

### 2. Trigger registration

Add one line at the bottom of [pipelines/dengue/lib/models/\_\_init\_\_.py](../pipelines/dengue/lib/models/__init__.py):

```python
from pipelines.dengue.lib.models import my_model as _my_model_mod  # noqa: F401, E402
```

This forces the module to be imported (and the `@register` decorator to run) whenever anything imports the registry.

### 3. Enable in YAML

```yaml
model:
  models: [nbr, tse, my_model]
```

That's it. No edits to `train_and_predict.py`.

---

## Adding a new ensemble strategy

Same pattern as models, in a separate registry.

### 1. Implement the strategy

`pipelines/dengue/lib/ensembles.py` already hosts the registry. Append your class:

```python
@register_ensemble("weighted")
class WeightedEnsemble:
    def combine(
        self, dfs: list[pd.DataFrame], *, spatial_col: str
    ) -> pd.DataFrame:
        # Combine predictions across DataFrames. Each df has a "model" column
        # identifying its source. Return a DataFrame with the same group-key
        # columns, a single "prediction" column, and "model" set to a label
        # of your choice (e.g. "weightedEnsemble").
        ...
```

Strategies must satisfy the `BaseEnsemble` Protocol — a single method `combine(dfs, *, spatial_col) -> pd.DataFrame`.

### 2. Enable in YAML

```yaml
model:
  models: [nbr, tse]
  ensemble: weighted
  output: ensemble
```

No edits elsewhere; the step looks up the strategy via `get_ensemble(cfg.ensemble)` at runtime.

---

## The `BaseModel` Protocol

A model is anything that satisfies this Protocol from [pipelines/dengue/lib/models/\_\_init\_\_.py](../pipelines/dengue/lib/models/__init__.py):

```python
@runtime_checkable
class BaseModel(Protocol):
    def predict(self, ctx: ModelContext) -> pd.DataFrame: ...
    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp: ...
```

`@runtime_checkable` means you can `isinstance(model, BaseModel)` to verify compliance.

### The two methods

- **`predict(ctx) → DataFrame`** — produce predictions. The returned DataFrame must contain at minimum `[spatial_col, recordDate, prediction, model]`. Return an empty DataFrame to indicate "no predictions possible" (the step will log a warning and skip the model rather than fail).
- **`threshold_to_date(ctx) → Timestamp`** — the cutoff date passed to [`zones.merge_predictions_thresholds`](../pipelines/dengue/lib/zones.py) for this model's predictions. NBR uses `pred_upto - 28d`; TSE uses `cutoff_case`. Pick the date that aligns with how far back your model's training horizon ends.

### `ModelContext`

```python
@dataclass
class ModelContext:
    merged_df: pd.DataFrame      # case + weather joined; use for weather-aware models
    case_df: pd.DataFrame        # case-only; use for case-only models
    cfg: TrainPredictConfig      # spatial_res, data_features, lag_temp, lag_rf, list_alpha, ...
    pred_upto: pd.Timestamp      # NBR's prediction horizon
    cutoff_case: pd.Timestamp    # last reliable case data date
```

Both DataFrames already have `recordDate` (datetime), `recordYear`, `recordMonth`, `ISOWeek` columns and the spatial column normalised to `cfg.spatial_res`.

---

## Verifying a new model works

```bash
# 1. Confirm registration
uv run python -c "from pipelines.dengue.lib.models import _REGISTRY; print(sorted(_REGISTRY))"

# 2. Confirm Protocol compliance
uv run python -c "
from pipelines.dengue.lib.models import BaseModel, get_model
assert isinstance(get_model('my_model'), BaseModel)
print('OK')
"

# 3. Run unit tests
uv run pytest tests/dengue/test_models.py -v

# 4. Run the pipeline end-to-end
DENGUE_CONFIG=configs/ka_district.yaml make run-dengue-pipeline DENGUE_RUN_ID=test-my-model
```

---

## Related

- Issue [#15](https://github.com/dsih-artpark/acestor/issues/15) — the original spec for this registry
- Issue [#17](https://github.com/dsih-artpark/acestor/issues/17) — the same pattern applied to threshold methods (in flight)
- [docs/CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) — full pipeline YAML reference
