"""Cutoff-date estimation.

Translated from GBA ``IdentifyCutoffDates.py``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

log = logging.getLogger(__name__)


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
    log.warning(
        "cutoffs: no date found where at least %d region(s) have data "
        "(max regions on any single date: %d across %d dates) → "
        "returning None, pipeline will raise downstream",
        min_regions,
        summary["n_regions"].max() if not summary.empty else 0,
        len(all_dates),
    )
    return None


def compute_prediction_dates(
    cutoff: pd.Timestamp,
    pred_upto: pd.Timestamp,
) -> list[str]:
    """Derive the list of prediction-week start dates."""
    n_weeks = max(0, int((pred_upto - cutoff).days / 7))
    if n_weeks == 0:
        log.warning(
            "cutoffs: prediction horizon collapsed to 0 weeks "
            "(cutoff=%s, pred_upto=%s, gap=%d days) → no prediction dates will be generated; "
            "maps will be empty",
            cutoff.date(),
            pred_upto.date(),
            (pred_upto - cutoff).days,
        )
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
        log.info(
            "cutoffs: case-data-limited branch — case cutoff (%s) is before weather+28d (%s); "
            "training cutoff=%s, pred_upto=%s",
            cutoff_case.date(),
            (cutoff_weather + timedelta(28)).date(),
            cutoff.date(),
            pred_upto.date(),
        )
    else:
        cutoff = cutoff_weather + timedelta(28)
        pred_upto = cutoff
        log.info(
            "cutoffs: weather-data-limited branch — case cutoff (%s) is NOT before weather+28d (%s); "
            "training cutoff=pred_upto=%s (prediction horizon may be zero)",
            cutoff_case.date(),
            (cutoff_weather + timedelta(28)).date(),
            cutoff.date(),
        )

    prediction_dates = compute_prediction_dates(cutoff, pred_upto)
    return cutoff, pred_upto, prediction_dates
