"""Random Forest Regression model.

Ported from vbd-modelbench/src/dengue_models/rf_model.py.
Trains on all weeks except the last 4, predicts those last 4.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

from pipelines.dengue.lib.models._shared import _lag
from pipelines.dengue.lib.models import _tuning as _tuning_mod

log = logging.getLogger(__name__)


def random_forest_regression(
    merged_df: pd.DataFrame,
    *,
    spatial_col: str,
    lag_temp: list[int],
    lag_rainfall: list[int],
    lag_humidity: list[int],
    lag_cases: list[int],
    years_to_exclude: list[int],
    years_to_include: list[int],
    predict_upto_date: pd.Timestamp,
    n_estimators: int = 200,
    max_depth: int = 10,
    min_samples_leaf: int = 2,
    max_features: str = "sqrt",
    random_state: int = 42,
    ctx: "ModelContext | None" = None,
) -> pd.DataFrame:
    """Train Random Forest on history and predict the last 4 weeks of weather data."""
    from pipelines.dengue.lib.zones import ret_na_filled_df

    df0 = ret_na_filled_df(
        merged_df, spatial_col=spatial_col, to_date=predict_upto_date
    )
    df0 = df0[df0["recordDate"] <= predict_upto_date].reset_index(drop=True)
    last_4 = df0["recordDate"].sort_values().unique()[-4:]
    mask = df0["recordDate"].isin(list(last_4))
    df0.loc[mask, "ISOWeek"] = df0.loc[mask, "recordDate"].apply(
        lambda x: datetime.isocalendar(x).week
    )

    df0 = _lag(df0, spatial_col, lag_temp, lag_rainfall, lag_humidity, lag_cases)

    lag_cols = (
        [f"temp_lag_{lg}" for lg in lag_temp]
        + [f"rainfall_lag_{lg}" for lg in lag_rainfall]
        + [f"relative_humidity_lag_{lg}" for lg in lag_humidity]
        + [f"case_lag_{lg}" for lg in lag_cases]
    )
    lag_cols = [c for c in lag_cols if c in df0.columns]

    train_data = df0[~df0["recordDate"].isin(last_4)].copy()
    if years_to_include:
        train_data = train_data[train_data["recordYear"].isin(years_to_include)]
    if years_to_exclude:
        train_data = train_data[~train_data["recordYear"].isin(years_to_exclude)]
    train_data = (
        train_data.dropna(subset=lag_cols + ["case"])
        .sort_values("recordDate")
        .reset_index(drop=True)
    )

    if train_data.empty:
        log.warning(
            "RF: training data empty after filtering — returning empty predictions"
        )
        return pd.DataFrame()

    X_train = train_data[lag_cols].reset_index(drop=True)
    y_train = train_data["case"].values

    test_data = df0[df0["recordDate"].isin(last_4)].copy().reset_index(drop=True)
    valid_mask = test_data[lag_cols].notna().all(axis=1)
    skipped = sorted(test_data.loc[~valid_mask, spatial_col].unique())
    if skipped:
        log.warning(
            "RF: skipping %d region(s) with NaN lag features — no weather coverage: %s",
            len(skipped),
            skipped,
        )
        test_data = test_data[valid_mask].copy().reset_index(drop=True)

    if test_data.empty:
        log.warning(
            "RF: no regions with valid lag features — returning empty predictions"
        )
        return pd.DataFrame()

    X_test = test_data[lag_cols].reset_index(drop=True)

    train_max_date = str(train_data["recordDate"].max().date())
    hp = (
        _get_rf_params(ctx, X_train.values, y_train, train_max_date)
        if ctx is not None
        else {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "min_samples_leaf": min_samples_leaf,
            "max_features": max_features,
        }
    )
    rf = RandomForestRegressor(
        **hp,
        random_state=random_state,
        n_jobs=-1,
    )
    rf.fit(X_train.values, y_train)

    test_data["prediction"] = np.maximum(0.0, rf.predict(X_test.values))
    test_data["recordDate"] = pd.to_datetime(test_data["recordDate"])
    test_data["model"] = "randomForestRegression"

    if (
        ctx is not None
        and getattr(ctx.cfg, "debug", False)
        and ctx.artifacts is not None
    ):
        _save_debug(
            ctx,
            "rf",
            X_train,
            y_train,
            X_test,
            test_data,
            rf.feature_importances_,
            lag_cols,
        )

    return test_data.reset_index(drop=True)


def _save_debug(
    ctx, model_name, X_train, y_train, X_test, test_data, importances, lag_cols
):
    import json as _json

    prefix = f"debug/{model_name}"
    train_df = pd.DataFrame(X_train, columns=lag_cols)
    train_df["case"] = y_train
    ctx.artifacts.write_text(train_df.to_csv(index=False), f"{prefix}/X_train.csv")
    ctx.artifacts.write_text(
        test_data[[c for c in test_data.columns]].to_csv(index=False),
        f"{prefix}/X_test_predictions.csv",
    )
    importance_dict = dict(sorted(zip(lag_cols, importances), key=lambda x: -x[1]))
    ctx.artifacts.write_text(
        _json.dumps(importance_dict, indent=2), f"{prefix}/feature_importance.json"
    )


def _get_rf_params(
    ctx: "ModelContext", X_train: Any, y_train: Any, train_max_date: str
) -> dict:
    """Load cached RF hyperparams or run Optuna tuning if needed."""
    defaults = {
        "n_estimators": 200,
        "max_depth": 10,
        "min_samples_leaf": 2,
        "max_features": "sqrt",
    }
    if ctx.artifacts is None:
        return defaults

    _log = ctx.log if ctx.log is not None else log

    cached = _tuning_mod.load_cached_params(ctx.artifacts, "rf")
    if cached is not None and not ctx.cfg.tune:
        _tuning_mod.check_fingerprint(
            ctx.artifacts, "rf", ctx.cfg, train_max_date, _log
        )
        _log.info(
            "RF: using cached hyperparameters (tuned %s, RMSE=%.4f) from hp/rf_best_params.json",
            cached["tuned_at"],
            cached["best_rmse"],
        )
        return cached["params"]

    _log.info(
        "RF: %s — running Optuna tuning (n_trials=%d)",
        (
            "tune=True, forcing retune"
            if ctx.cfg.tune
            else "no cached hyperparameters found"
        ),
        ctx.cfg.n_trials,
    )
    params, rmse = _tuning_mod.tune_rf(X_train, y_train, n_trials=ctx.cfg.n_trials)
    tuned_at = pd.Timestamp.now().strftime("%Y-%m-%d")
    _tuning_mod.save_params(
        ctx.artifacts,
        "rf",
        params,
        rmse=rmse,
        n_trials=ctx.cfg.n_trials,
        tuned_at=tuned_at,
    )
    _tuning_mod.save_fingerprint(
        ctx.artifacts,
        "rf",
        _tuning_mod.compute_fingerprint(ctx.cfg, train_max_date),
    )
    _log.info(
        "RF: tuning complete — best RMSE=%.4f, params saved to hp/rf_best_params.json",
        rmse,
    )
    return params


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("rf")
class RFModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return random_forest_regression(
            ctx.merged_df,
            spatial_col=ctx.cfg.spatial_res,
            lag_temp=ctx.cfg.lag_temp,
            lag_rainfall=ctx.cfg.lag_rainfall,
            lag_humidity=ctx.cfg.lag_humidity,
            lag_cases=ctx.cfg.lag_cases,
            years_to_exclude=ctx.cfg.years_to_exclude,
            years_to_include=ctx.cfg.years_to_include,
            predict_upto_date=ctx.pred_upto,
            ctx=ctx,
        )

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        return ctx.pred_upto - pd.Timedelta(days=28)
