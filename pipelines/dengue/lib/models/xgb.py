"""XGBoost Regression model.

Ported from vbd-modelbench/src/dengue_models/xgb_model.py.
Trains on all weeks except the last 4, predicts those last 4.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from pipelines.dengue.lib.models._shared import _lag
from pipelines.dengue.lib.models import _tuning as _tuning_mod

log = logging.getLogger(__name__)


def xgboost_regression(
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
    n_estimators: int = 300,
    learning_rate: float = 0.05,
    max_depth: int = 5,
    min_child_weight: int = 5,
    subsample: float = 0.8,
    colsample_bytree: float = 0.8,
    gamma: float = 0.1,
    reg_alpha: float = 0.1,
    reg_lambda: float = 5.0,
    random_state: int = 42,
    ctx: "ModelContext | None" = None,
) -> pd.DataFrame:
    """Train XGBoost on history and predict the last 4 weeks of weather data."""
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
            "XGB: training data empty after filtering — returning empty predictions"
        )
        return pd.DataFrame()

    X_train = train_data[lag_cols].reset_index(drop=True)
    y_train = train_data["case"].values

    test_data = df0[df0["recordDate"].isin(last_4)].copy().reset_index(drop=True)
    valid_mask = test_data[lag_cols].notna().all(axis=1)
    skipped = sorted(test_data.loc[~valid_mask, spatial_col].unique())
    if skipped:
        log.warning(
            "XGB: skipping %d region(s) with NaN lag features — no weather coverage: %s",
            len(skipped),
            skipped,
        )
        test_data = test_data[valid_mask].copy().reset_index(drop=True)

    if test_data.empty:
        log.warning(
            "XGB: no regions with valid lag features — returning empty predictions"
        )
        return pd.DataFrame()

    X_test = test_data[lag_cols].reset_index(drop=True)

    train_max_date = str(train_data["recordDate"].max().date())
    hp = (
        _get_xgb_params(ctx, X_train.values, y_train, train_max_date)
        if ctx is not None
        else {
            "n_estimators": n_estimators,
            "learning_rate": learning_rate,
            "max_depth": max_depth,
            "min_child_weight": min_child_weight,
            "subsample": subsample,
            "colsample_bytree": colsample_bytree,
            "gamma": gamma,
            "reg_alpha": reg_alpha,
            "reg_lambda": reg_lambda,
        }
    )
    xgb_model = XGBRegressor(
        **hp,
        random_state=random_state,
        n_jobs=-1,
        verbosity=0,
    )
    xgb_model.fit(X_train.values, y_train)

    test_data["prediction"] = np.maximum(0.0, xgb_model.predict(X_test.values))
    test_data["recordDate"] = pd.to_datetime(test_data["recordDate"])
    test_data["model"] = "xgboostRegression"

    if (
        ctx is not None
        and getattr(ctx.cfg, "debug", False)
        and ctx.artifacts is not None
    ):
        from pipelines.dengue.lib.models.rf import _save_debug

        _save_debug(
            ctx,
            "xgb",
            X_train,
            y_train,
            X_test,
            test_data,
            xgb_model.feature_importances_,
            lag_cols,
        )

    return test_data.reset_index(drop=True)


def _get_xgb_params(
    ctx: "ModelContext", X_train: Any, y_train: Any, train_max_date: str
) -> dict:
    """Load cached XGB hyperparams or run Optuna tuning if needed."""
    defaults = {
        "n_estimators": 300,
        "learning_rate": 0.05,
        "max_depth": 5,
        "min_child_weight": 5,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "gamma": 0.1,
        "reg_alpha": 0.1,
        "reg_lambda": 5.0,
    }
    if ctx.artifacts is None:
        return defaults

    _log = ctx.log if ctx.log is not None else log

    cached = _tuning_mod.load_cached_params(ctx.artifacts, "xgb")
    if cached is not None and not ctx.cfg.tune:
        _tuning_mod.check_fingerprint(
            ctx.artifacts, "xgb", ctx.cfg, train_max_date, _log
        )
        _log.info(
            "XGB: using cached hyperparameters (tuned %s, RMSE=%.4f) from hp/xgb_best_params.json",
            cached["tuned_at"],
            cached["best_rmse"],
        )
        return cached["params"]

    _log.info(
        "XGB: %s — running Optuna tuning (n_trials=%d)",
        (
            "tune=True, forcing retune"
            if ctx.cfg.tune
            else "no cached hyperparameters found"
        ),
        ctx.cfg.n_trials,
    )
    params, rmse = _tuning_mod.tune_xgb(X_train, y_train, n_trials=ctx.cfg.n_trials)
    tuned_at = pd.Timestamp.now().strftime("%Y-%m-%d")
    _tuning_mod.save_params(
        ctx.artifacts,
        "xgb",
        params,
        rmse=rmse,
        n_trials=ctx.cfg.n_trials,
        tuned_at=tuned_at,
    )
    _tuning_mod.save_fingerprint(
        ctx.artifacts,
        "xgb",
        _tuning_mod.compute_fingerprint(ctx.cfg, train_max_date),
    )
    _log.info(
        "XGB: tuning complete — best RMSE=%.4f, params saved to hp/xgb_best_params.json",
        rmse,
    )
    return params


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("xgb")
class XGBModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return xgboost_regression(
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
