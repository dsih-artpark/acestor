"""Shared utilities used by multiple model implementations."""

from __future__ import annotations

import pandas as pd
from sklearn.preprocessing import OneHotEncoder


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


def _one_hot(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    if cols is None:
        cols = ["ISOWeek"]
    enc = OneHotEncoder(sparse_output=False)
    encoded = enc.fit_transform(df[cols])
    return pd.DataFrame(encoded, columns=enc.get_feature_names_out(cols))
