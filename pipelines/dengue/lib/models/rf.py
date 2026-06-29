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

from pipelines.dengue.lib.models._shared import _lag, recursive_forecast
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
    """Train Random Forest on history, then recursively forecast the next 4 weeks.

    Weather/iso lags for the forecast weeks are known ahead of time; case lags are
    filled from earlier weeks' own predictions (see _shared.recursive_forecast).
    """
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

    debug_on = bool(
        ctx is not None
        and getattr(ctx.cfg, "debug", False)
        and ctx.artifacts is not None
    )
    preds, debug_rows = recursive_forecast(
        rf,
        df0,
        spatial_col=spatial_col,
        feature_cols=lag_cols,
        lag_cases=lag_cases,
        future_dates=sorted(pd.to_datetime(d) for d in last_4),
        clip_multiplier=(
            getattr(ctx.cfg, "clip_multiplier", None) if ctx is not None else None
        ),
        train_max=float(np.nanmax(y_train)) if len(y_train) else None,
        debug=debug_on,
    )

    if preds.empty:
        log.warning(
            "RF: no regions with valid lag features — returning empty predictions"
        )
        return pd.DataFrame()

    preds["model"] = "randomForestRegression"
    if debug_on:
        _save_debug(
            ctx,
            "rf",
            X_train,
            y_train,
            preds,
            debug_rows,
            rf.feature_importances_,
            lag_cols,
        )

    return preds.reset_index(drop=True)


def _save_debug(
    ctx, model_name, X_train, y_train, preds, debug_rows, importances, lag_cols
):
    import json as _json

    prefix = f"{ctx.run_id}/debug/{model_name}"
    train_df = pd.DataFrame(X_train, columns=lag_cols)
    train_df["case"] = y_train
    ctx.artifacts.write_text(train_df.to_csv(index=False), f"{prefix}/X_train.csv")
    ctx.artifacts.write_text(preds.to_csv(index=False), f"{prefix}/predictions.csv")
    if debug_rows:
        # Per-horizon recursive trace: each case-lag value + source (observed/
        # predicted/zero_pad) and raw vs clipped prediction.
        ctx.artifacts.write_text(
            pd.DataFrame(debug_rows).to_csv(index=False),
            f"{prefix}/recursive_trace.csv",
        )
    importance_dict = dict(
        sorted(zip(lag_cols, [float(v) for v in importances]), key=lambda x: -x[1])
    )
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

    region_type = getattr(ctx.cfg, "spatial_res", "") or ""
    cached = _tuning_mod.load_cached_params(ctx.artifacts, "rf", region_type)

    # tune="never" — trust the cache regardless of fingerprint (hindcast /
    # operational mode: tune once, reuse across vintages). Fail loud if the
    # cache is missing rather than silently slipping back into auto-retune.
    if ctx.cfg.tune == "never":
        if cached is None:
            raise FileNotFoundError(
                f"RF [{region_type or '?'}]: tune='never' but no cached "
                f"hyperparameters found at {_tuning_mod.hp_cache_path('rf', region_type)!r}. "
                f"Prime the cache with a one-time run using tune=true."
            )
        _log.info(
            "RF [%s]: tune='never' — using cached hyperparameters "
            "(tuned %s, RMSE=%.4f) without fingerprint check",
            region_type or "?",
            cached["tuned_at"],
            cached["best_rmse"],
        )
        return cached["params"]

    fingerprint_ok = _tuning_mod.check_fingerprint(
        ctx.artifacts, "rf", ctx.cfg, train_max_date, _log
    )
    if cached is not None and not ctx.cfg.tune and fingerprint_ok:
        _log.info(
            "RF [%s]: using cached hyperparameters (tuned %s, RMSE=%.4f)",
            region_type or "?",
            cached["tuned_at"],
            cached["best_rmse"],
        )
        return cached["params"]

    if ctx.cfg.tune is True:
        reason = "tune=True, forcing retune"
    elif cached is None:
        reason = "no cached hyperparameters found"
    else:
        reason = "fingerprint stale — auto-retuning"
    _log.info(
        "RF [%s]: %s — running Optuna tuning (n_trials=%d)",
        region_type or "?",
        reason,
        ctx.cfg.n_trials,
    )
    params, rmse = _tuning_mod.tune_rf(X_train, y_train, n_trials=ctx.cfg.n_trials)
    tuned_at = pd.Timestamp.now().strftime("%Y-%m-%d")
    _tuning_mod.save_params(
        ctx.artifacts,
        "rf",
        region_type,
        params,
        rmse=rmse,
        n_trials=ctx.cfg.n_trials,
        tuned_at=tuned_at,
    )
    _tuning_mod.save_fingerprint(
        ctx.artifacts,
        "rf",
        region_type,
        _tuning_mod.compute_fingerprint(ctx.cfg, train_max_date),
    )
    _log.info(
        "RF [%s]: tuning complete — best RMSE=%.4f, params saved to %s",
        region_type,
        rmse,
        _tuning_mod.hp_cache_path("rf", region_type),
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
