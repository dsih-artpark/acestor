"""Negative Binomial Regression model.

Translated from GBA ``models/nbr.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import statsmodels.api as sm
from sklearn.preprocessing import MinMaxScaler

from pipelines.dengue.lib.models._shared import _lag, _one_hot

log = logging.getLogger(__name__)


def _filter_features(
    df: pd.DataFrame,
    feature_cols: list[str],
    spatial_col: str,
    years_to_exclude: list[int],
    years_to_include: list[int],
) -> pd.DataFrame:
    keep = [c for c in feature_cols if c in df.columns]
    keep += [
        c
        for c in ["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"]
        if c in df.columns
    ]
    keep.append(spatial_col)
    keep = list(dict.fromkeys(keep))
    out = df[keep].copy()
    if years_to_include:
        out = out[out["recordYear"].isin(years_to_include)]
    if years_to_exclude:
        out = out[~out["recordYear"].isin(years_to_exclude)]
    return out.dropna().reset_index(drop=True)


def _rescale(
    df: pd.DataFrame, scaler: MinMaxScaler | None = None
) -> tuple[pd.DataFrame, MinMaxScaler]:
    feat_cols = [
        c
        for c in ["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"]
        if c in df.columns
    ]
    feats = df[feat_cols].dropna(axis=1, how="all")
    if scaler is None:
        scaler = MinMaxScaler()
        scaled = pd.DataFrame(scaler.fit_transform(feats), columns=feats.columns)
    else:
        scaled = pd.DataFrame(scaler.transform(feats), columns=feats.columns)
    return scaled, scaler


def negative_binomial_regression(
    merged_df: pd.DataFrame,
    *,
    spatial_col: str,
    feature_cols: list[str],
    lag_temp: list[int],
    lag_rf: list[int],
    years_to_exclude: list[int],
    years_to_include: list[int],
    predict_upto_date: pd.Timestamp,
) -> pd.DataFrame:
    """Run negative-binomial regression and return predictions for the last 4 weeks of weather."""
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

    train_data = df0[~df0["recordDate"].isin(last_4)].copy()
    filtered = _filter_features(
        train_data, feature_cols, spatial_col, years_to_exclude, years_to_include
    )
    encoded = _one_hot(filtered)
    scaled, scaler = _rescale(filtered)
    X_train = sm.add_constant(pd.concat([scaled, encoded], axis=1))
    y_train = filtered["case"]

    model = sm.GLM(y_train, X_train, family=sm.families.NegativeBinomial(alpha=1.0))
    results = model.fit(method="lbfgs")

    test_data = df0[df0["recordDate"].isin(last_4)].copy().reset_index(drop=True)

    lag_feat_cols = [
        c
        for c in ["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"]
        if c in test_data.columns
    ]
    # Drop regions where any lag feature is NaN for all prediction dates —
    # these have no weather coverage and MinMaxScaler.transform() would crash.
    valid_mask = test_data[lag_feat_cols].notna().all(axis=1)
    skipped_regions = sorted(test_data.loc[~valid_mask, spatial_col].unique())
    if skipped_regions:
        log.warning(
            "NBR: skipping %d region(s) with NaN lag features — no weather coverage "
            "(will appear white/hatched on map): %s",
            len(skipped_regions),
            skipped_regions,
        )
        test_data = test_data[valid_mask].copy().reset_index(drop=True)

    if test_data.empty:
        log.warning(
            "NBR: no regions have valid lag features — returning empty predictions"
        )
        return pd.DataFrame()

    test_encoded = _one_hot(test_data)
    test_scaled, _ = _rescale(test_data, scaler)
    X_test = sm.add_constant(pd.concat([test_scaled, test_encoded], axis=1))

    for col in set(X_train.columns) - set(X_test.columns):
        X_test[col] = 0
    X_test = X_test[X_train.columns]

    test_data["prediction"] = results.predict(X_test).values
    test_data["recordDate"] = pd.to_datetime(test_data["recordDate"])
    test_data["model"] = "negativeBinomialRegression"
    return test_data.reset_index(drop=True)


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("nbr")
class NBRModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return negative_binomial_regression(
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
