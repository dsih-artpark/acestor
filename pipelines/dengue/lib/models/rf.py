"""Random Forest Regression model.

Ported from vbd-modelbench/src/dengue_models/rf_model.py.
Trains on all weeks except the last 4, predicts those last 4.
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

from pipelines.dengue.lib.models._shared import _lag, _one_hot

log = logging.getLogger(__name__)

_LAG_FEAT_COLS = ["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"]


def random_forest_regression(
    merged_df: pd.DataFrame,
    *,
    spatial_col: str,
    feature_cols: list[str],
    lag_temp: list[int],
    lag_rf: list[int],
    years_to_exclude: list[int],
    years_to_include: list[int],
    predict_upto_date: pd.Timestamp,
    n_estimators: int = 200,
    max_depth: int = 10,
    min_samples_leaf: int = 2,
    max_features: str = "sqrt",
    random_state: int = 42,
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

    df0 = _lag(df0, spatial_col, lag_temp, lag_rf)

    lag_cols = [c for c in _LAG_FEAT_COLS if c in df0.columns]

    train_data = df0[~df0["recordDate"].isin(last_4)].copy()
    if years_to_include:
        train_data = train_data[train_data["recordYear"].isin(years_to_include)]
    if years_to_exclude:
        train_data = train_data[~train_data["recordYear"].isin(years_to_exclude)]
    train_data = train_data.dropna(subset=lag_cols + ["case"]).reset_index(drop=True)

    if train_data.empty:
        log.warning(
            "RF: training data empty after filtering — returning empty predictions"
        )
        return pd.DataFrame()

    encoded_train = _one_hot(train_data)
    X_train = pd.concat(
        [train_data[lag_cols].reset_index(drop=True), encoded_train], axis=1
    )
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

    encoded_test = _one_hot(test_data)
    X_test = pd.concat(
        [test_data[lag_cols].reset_index(drop=True), encoded_test], axis=1
    )
    for col in set(X_train.columns) - set(X_test.columns):
        X_test[col] = 0
    X_test = X_test[X_train.columns]

    rf = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        max_features=max_features,
        random_state=random_state,
        n_jobs=-1,
    )
    rf.fit(X_train.values, y_train)

    test_data["prediction"] = np.maximum(0.0, rf.predict(X_test.values))
    test_data["recordDate"] = pd.to_datetime(test_data["recordDate"])
    test_data["model"] = "randomForestRegression"
    return test_data.reset_index(drop=True)


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("rf")
class RFModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return random_forest_regression(
            ctx.merged_df,
            spatial_col=ctx.cfg.spatial_res,
            feature_cols=ctx.cfg.data_features,
            lag_temp=ctx.cfg.lag_temp,
            lag_rf=ctx.cfg.lag_rf,
            years_to_exclude=ctx.cfg.years_to_exclude,
            years_to_include=ctx.cfg.years_to_include,
            predict_upto_date=ctx.pred_upto,
        )

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        return ctx.pred_upto - pd.Timedelta(days=28)
