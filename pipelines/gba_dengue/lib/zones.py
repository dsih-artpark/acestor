"""Zone classification and NA-fill helpers.

Translated from GBA ``utils.py`` (``retNAfilledDF``, ``classifyIntoZones``,
``AssignZone``, ``retThresholdPairs``, ``compute_thresholds``).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# NA fill helpers
# ---------------------------------------------------------------------------


def _gen_date_list(df_temp: pd.DataFrame, to_date) -> list:
    date_list = list(df_temp["recordDate"].unique())
    return sorted(
        pd.date_range(
            date_list[0] if date_list else to_date, to_date, freq="7D"
        ).tolist()
    )


def _gen_missing_date_rows(df_temp: pd.DataFrame, to_date) -> pd.DataFrame:
    dates = _gen_date_list(df_temp, to_date)
    return df_temp.set_index("recordDate").reindex(dates).reset_index()


def ret_na_filled_df(
    df: pd.DataFrame,
    *,
    spatial_col: str,
    to_date=None,
) -> pd.DataFrame:
    """Fill all missing weekly dates across all regions with NaN rows."""
    if to_date is None:
        to_date = datetime.now().date()
    if isinstance(to_date, pd.Timestamp):
        to_date = to_date.date() if hasattr(to_date, "date") else to_date
    parts = [
        _gen_missing_date_rows(df[df[spatial_col] == v], to_date)
        for v in df[spatial_col].unique()
    ]
    filled = pd.concat(parts, ignore_index=True)
    filled[spatial_col] = filled[spatial_col].ffill()
    filled["recordDate"] = pd.to_datetime(filled["recordDate"])
    filled["recordYear"] = filled["recordDate"].dt.year
    filled["recordMonth"] = filled["recordDate"].dt.month
    return filled


# ---------------------------------------------------------------------------
# Threshold computation
# ---------------------------------------------------------------------------


def mu_sigma(
    case_data: pd.DataFrame,
    *,
    spatial_col: str,
    to_date=None,
) -> pd.DataFrame:
    """Compute mean and std using both historical and previous-N-weeks methods."""
    from pipelines.gba_dengue.lib.thresholds import (
        historical_threshold_params,
        prev_nweeks_threshold_params,
    )

    df0 = ret_na_filled_df(case_data, spatial_col=spatial_col, to_date=to_date)
    agg = df0.sort_values([spatial_col, "recordDate"]).reset_index(drop=True)

    # Threshold helpers expect columns named ``region_id`` and ``date``.
    agg_for_thresh = agg.rename(
        columns={spatial_col: "region_id", "recordDate": "date"}
    )
    hist = historical_threshold_params(agg_for_thresh)
    prev = prev_nweeks_threshold_params(agg_for_thresh)
    return pd.concat([hist, prev], ignore_index=True)


def compute_thresholds(
    case_data: pd.DataFrame,
    *,
    spatial_col: str,
    list_alpha: list[float],
    to_date=None,
) -> pd.DataFrame:
    """Compute thresholds at each alpha level."""
    df = mu_sigma(case_data, spatial_col=spatial_col, to_date=to_date)
    thresholds = df.copy()
    thresholds["Zero"] = 0.0
    thresholds["Inf"] = np.inf

    alphas = [0.0] + list(list_alpha)
    for a in alphas:
        thresholds[f"T{a:.2f}"] = thresholds["Mean"] + a * thresholds["StdDev"]

    epsilon = 1e-6
    for i, a in enumerate(alphas[1:], start=1):
        thresholds[f"T{a:.2f}"] += i * epsilon

    return thresholds


# ---------------------------------------------------------------------------
# Zone assignment
# ---------------------------------------------------------------------------


def ret_threshold_pairs(df: pd.DataFrame) -> list[tuple[str, str]]:
    """Build (lower, upper) column-name pairs from threshold columns."""
    t_cols = [c for c in df.columns if (("T" in c and "." in c) or "Thresh" in c)]
    cols = [None] + t_cols + [None]
    pairs = []
    for lo, hi in zip(cols[:-1], cols[1:]):
        pairs.append(("Zero" if lo is None else lo, "Inf" if hi is None else hi))
    return pairs


def assign_zone(df: pd.DataFrame, pairs: list[tuple[str, str]]) -> pd.DataFrame:
    """Assign a numeric predictionZone based on threshold pairs."""
    out = df.copy()
    for i, (lo, hi) in enumerate(pairs):
        mask = (out["prediction"] >= out[lo]) & (out["prediction"] < out[hi])
        out.loc[mask, "predictionZone"] = i + 1
    return out


def classify_into_zones(
    df_predictions: pd.DataFrame,
    *,
    spatial_col: str,
) -> pd.DataFrame:
    """Assign risk zones and add metadata columns."""
    pairs = ret_threshold_pairs(df_predictions)
    out = assign_zone(df_predictions, pairs)
    out["dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
    out["regionID"] = out[spatial_col]
    return out


def merge_predictions_thresholds(
    case_data: pd.DataFrame,
    model_pred: pd.DataFrame,
    *,
    spatial_col: str,
    list_alpha: list[float],
    to_date=None,
    n_days: int = 28,
) -> pd.DataFrame:
    """Merge model predictions with computed thresholds."""
    thresholds = compute_thresholds(
        case_data, spatial_col=spatial_col, list_alpha=list_alpha, to_date=to_date
    )
    if to_date:
        thresholds = thresholds[thresholds["date"] == pd.Timestamp(to_date)]

    t_cols = [c for c in thresholds.columns if c.startswith("T") and "." in c]
    thresh_keep = [
        "region_id",
        "ISOWeek",
        "threshold_method",
        "Mean",
        "StdDev",
        "Zero",
        "Inf",
    ] + t_cols
    thresh_keep = [c for c in thresh_keep if c in thresholds.columns]

    pred_keep = [spatial_col, "recordDate", "prediction", "model"]
    pred_keep = [c for c in pred_keep if c in model_pred.columns]

    merged = model_pred[pred_keep].merge(
        thresholds[thresh_keep].rename(columns={"region_id": spatial_col}),
        on=spatial_col,
        how="left",
    )
    merged["startDatePredictedWeek"] = merged["recordDate"]
    merged["dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
    merged["regionID"] = merged[spatial_col]
    # SOT prediction CSVs use camelCase; assess_thresholds / maps expect thresholdMethod.
    if "threshold_method" in merged.columns:
        merged = merged.rename(columns={"threshold_method": "thresholdMethod"})
    return merged.sort_values([spatial_col, "recordDate"]).reset_index(drop=True)
