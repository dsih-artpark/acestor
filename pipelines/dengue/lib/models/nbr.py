"""Negative Binomial Regression model.

Translated from GBA ``models/nbr.py``.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import statsmodels.api as sm
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder


def _lag(
    df: pd.DataFrame, spatial_col: str, lag_temp: list[int], lag_rf: list[int]
) -> pd.DataFrame:
    for lg in lag_temp:
        df[f"temp_lag_{lg}"] = df.groupby(spatial_col)["t2m_mean"].shift(lg)
    for lg in lag_rf:
        df[[f"rainfall_lag_{lg}", f"relative_humidity_lag_{lg}"]] = df.groupby(
            spatial_col
        )[["tp_sum", "d2m_mean"]].shift(lg)
    return df


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


def _one_hot(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    if cols is None:
        cols = ["ISOWeek"]
    enc = OneHotEncoder(sparse_output=False)
    encoded = enc.fit_transform(df[cols])
    return pd.DataFrame(encoded, columns=enc.get_feature_names_out(cols))


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
