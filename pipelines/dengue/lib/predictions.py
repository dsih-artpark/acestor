"""Prediction combination helpers.

Translated from GBA ``CombineAllPredictions.py`` and ``run_zone.py`` / ``run_corp.py``.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

COLS_OF_INTEREST = [
    "dateOfComputingPrediction",
    "startDatePredictedWeek",
    "regionID",
    "prediction",
    "thresholdMethod",
    "predictionZone",
    "model",
]


def get_month_year_range(dates: list[pd.Timestamp]) -> str:
    """Generate a human-readable month range string from a list of dates."""
    unique_months = sorted({(d.year, d.month) for d in dates})
    if not unique_months:
        return ""
    (sy, sm), (ey, em) = unique_months[0], unique_months[-1]

    def fmt_month_year(y: int, m: int) -> str:
        return pd.Timestamp(year=y, month=m, day=1).strftime("%b %Y")

    def fmt_month(m: int) -> str:
        return pd.Timestamp(year=2000, month=m, day=1).strftime("%b")

    if len(unique_months) == 1:
        return fmt_month_year(sy, sm)
    if sy == ey:
        return f"{fmt_month(sm)} - {fmt_month(em)} {sy}"
    return f"{fmt_month(sm)} {sy} - {fmt_month(em)} {ey}"


def ensemble_predictions(
    dfs: list[pd.DataFrame],
    *,
    spatial_col: str,
) -> pd.DataFrame:
    """Merge NBR + TSE predictions into an ensemble by taking the mean prediction."""
    combined = pd.concat(dfs, ignore_index=True)
    rows_before = len(combined)
    group_cols = [c for c in combined.columns if c not in ("prediction", "model")]

    # Warn about any NaN values in groupby keys — pandas silently drops those rows.
    nan_key_cols = [c for c in group_cols if combined[c].isna().any()]
    if nan_key_cols:
        for col in nan_key_cols:
            affected = combined.loc[combined[col].isna(), spatial_col].unique().tolist()
            log.warning(
                "ensemble_predictions: column '%s' has NaN values for %d region(s) %s "
                "— pandas groupby will silently DROP these rows, removing them from the "
                "ensemble output entirely",
                col,
                len(affected),
                affected,
            )

    ensembled = combined.groupby(group_cols)["prediction"].mean().reset_index()
    rows_after = len(ensembled)
    if rows_before != rows_after:
        log.warning(
            "ensemble_predictions: %d row(s) were silently dropped by groupby "
            "(%d → %d) due to NaN keys in columns: %s",
            rows_before - rows_after,
            rows_before,
            rows_after,
            nan_key_cols,
        )

    ensembled["model"] = "ensembleModel"
    return ensembled


def combine_all_predictions(
    prediction_dfs: list[pd.DataFrame],
    prediction_dates: list[str],
) -> pd.DataFrame:
    """Concatenate all region-level prediction CSVs and filter to prediction dates."""
    cols = (
        [c for c in COLS_OF_INTEREST if c in prediction_dfs[0].columns]
        if prediction_dfs
        else COLS_OF_INTEREST
    )
    combined = pd.concat([df[cols] for df in prediction_dfs], ignore_index=True)
    if prediction_dates:
        combined = combined[
            (combined["startDatePredictedWeek"] >= prediction_dates[0])
            & (combined["startDatePredictedWeek"] <= prediction_dates[-1])
        ].reset_index(drop=True)
    return combined
