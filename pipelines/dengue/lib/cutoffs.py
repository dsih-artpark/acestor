"""Cutoff-date estimation.

Translated from GBA ``IdentifyCutoffDates.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd


def estimate_cutoff_date(
    df: pd.DataFrame, min_regions: int = 10
) -> pd.Timestamp | None:
    """Find the most recent date where at least ``min_regions`` have data."""
    summary = df.groupby("recordDate").size().reset_index(name="n_regions")
    all_dates = sorted(df["recordDate"].unique(), reverse=True)
    for d in all_dates:
        n = summary.loc[summary["recordDate"] == d, "n_regions"].iloc[0]
        if n >= min_regions:
            return pd.Timestamp(d)
    return None


def compute_prediction_dates(
    cutoff: pd.Timestamp,
    pred_upto: pd.Timestamp,
) -> list[str]:
    """Derive the list of prediction-week start dates."""
    n_weeks = max(0, int((pred_upto - cutoff).days / 7))
    raw = [(pred_upto - timedelta(days=i * 7)).date() for i in range(n_weeks)]
    raw = sorted(raw)[-4:]
    return [d.strftime("%Y-%m-%d") for d in raw]


def identify_cutoff_dates(
    cutoff_case: pd.Timestamp | None,
    cutoff_weather: pd.Timestamp | None,
) -> tuple[pd.Timestamp, pd.Timestamp, list[str]]:
    """Determine the training cutoff, prediction horizon, and prediction dates.

    Returns ``(cutoff, pred_upto, prediction_dates)``.
    """
    if cutoff_case is None or cutoff_weather is None:
        raise ValueError(
            "Cannot identify cutoff dates when case or weather cutoff is None."
        )

    if cutoff_case < (cutoff_weather + timedelta(28)):
        cutoff = cutoff_case
        pred_upto = cutoff_weather + timedelta(28)
    else:
        cutoff = cutoff_weather + timedelta(28)
        pred_upto = cutoff

    prediction_dates = compute_prediction_dates(cutoff, pred_upto)
    return cutoff, pred_upto, prediction_dates
