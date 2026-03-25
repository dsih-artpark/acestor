"""Time-Series Extrapolation model.

Translated from GBA ``models/tse.py``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd


def _process_region(
    df: pd.DataFrame, spatial_col: str, predict_upto_date, years_to_exclude: list[int]
) -> pd.DataFrame:
    df = df.copy()
    df["IsNaN"] = pd.isna(df["case"])
    df["equivCase"] = df.apply(lambda r: r["case"] if not r["IsNaN"] else -999, axis=1)
    df["CountNaN"] = (
        df["equivCase"]
        .rolling(window=4)
        .apply(lambda x: np.nan if sum(v == -999 for v in x) > 2 else 0)
    )
    df["equivCase"] = df["equivCase"].apply(lambda x: 0 if x == -999 else x)
    df["Avg"] = df["equivCase"].rolling(window=4).mean()
    df["4wMovingAvg"] = df[["CountNaN", "Avg"]].apply(
        lambda x: x.iloc[0] + x.iloc[1], axis=1
    )
    df["MaxCaseMonthlyHistorical"] = df.groupby([spatial_col, "recordMonth"])[
        "equivCase"
    ].transform("max")
    df = df[~df["recordYear"].isin(years_to_exclude)]
    df = df[df["recordDate"] <= pd.to_datetime(predict_upto_date)]
    return df.sort_values("recordDate").reset_index(drop=True)


def linear_extrapolation(
    df: pd.DataFrame,
    *,
    spatial_col: str,
    years_to_exclude: list[int],
    predict_upto_date: pd.Timestamp,
) -> pd.DataFrame:
    """Extrapolate case counts 2 weeks ahead using a linear trend on the 4-week moving average."""
    from pipelines.gba_dengue.lib.zones import ret_na_filled_df

    to_date = predict_upto_date - pd.Timedelta(days=14)
    df = ret_na_filled_df(df, spatial_col=spatial_col, to_date=to_date)
    df = df.sort_values(["recordDate", spatial_col]).reset_index(drop=True)

    rows = []
    skipped = []

    for region_name, region_data in df.groupby(spatial_col):
        region_data = _process_region(
            region_data, spatial_col, predict_upto_date, years_to_exclude
        )
        if len(region_data) < 2:
            skipped.append(region_name)
            continue

        last_date = region_data["recordDate"].iloc[-1]
        second_last_date = region_data["recordDate"].iloc[-2]

        # SOT compares calendar dates; normalize so we never mix pd.Timestamp with datetime.date.
        to_date_day = pd.Timestamp(to_date).normalize().date()
        last_day = pd.Timestamp(last_date).normalize().date()
        second_last_day = pd.Timestamp(second_last_date).normalize().date()

        if (to_date_day - last_day) <= timedelta(days=15) and (
            to_date_day - second_last_day
        ) <= timedelta(days=15):
            date_diff = (last_date - second_last_date).days
            avg_diff = (
                region_data["4wMovingAvg"].iloc[-1]
                - region_data["4wMovingAvg"].iloc[-2]
            )

            for i in range(1, 3):
                future_date = last_date + timedelta(days=7 * i)
                pred = max(
                    0,
                    region_data["4wMovingAvg"].iloc[-1]
                    + (avg_diff / date_diff) * (7 * i),
                )
                # month_max = region_data[region_data["recordMonth"] == future_date.month]["MaxCaseMonthlyHistorical"].iloc[0]

                rows.append(
                    {
                        spatial_col: region_name,
                        "recordDate": future_date,
                        "prediction": pred,
                        # "MaxCaseMonthlyHistorical": month_max,
                        "model": "timeSeriesExtrapolation",
                    }
                )
        else:
            skipped.append(region_name)

    if skipped:
        logging.warning("Regions without enough data for TSE: %s", skipped)

    return (
        pd.DataFrame(rows)
        if rows
        else pd.DataFrame(columns=[spatial_col, "recordDate", "prediction", "model"])
    )
