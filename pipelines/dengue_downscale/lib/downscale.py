"""Pure computation: parent→child spatial disaggregation via rolling case shares."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def build_parent_child_mapping(geojson_dir: Path) -> dict[str, str]:
    """Read all .geojson files in geojson_dir and return {child_id: parent_id}.

    Each geojson must be a FeatureCollection whose first feature's properties
    contain 'region_id' and 'parent'. Files missing either field are skipped.
    """
    mapping: dict[str, str] = {}
    for path in sorted(geojson_dir.glob("*.geojson")):
        data = json.loads(path.read_text())
        features = data.get("features", [])
        if not features:
            log.warning(
                "build_parent_child_mapping: no features in %s, skipping", path.name
            )
            continue
        props = features[0].get("properties", {})
        child_id = props.get("region_id")
        parent_id = props.get("parent")
        if not child_id or not parent_id:
            log.warning(
                "build_parent_child_mapping: missing region_id or parent in %s, skipping",
                path.name,
            )
            continue
        mapping[child_id] = parent_id
    return mapping


def compute_shares(
    cases_df: pd.DataFrame,
    parent_id: str,
    child_ids: list[str],
    as_of_date: pd.Timestamp,
    window_weeks: int,
) -> dict[str, float]:
    """Compute each child's share of the parent's total cases over the last window_weeks.

    cases_df columns: region_id (str), date (Timestamp or date string), case_count (numeric)

    The window is (as_of_date - window_weeks, as_of_date] — exclusive lower bound.
    Falls back to uniform shares when total cases in window == 0.
    """
    cutoff_start = as_of_date - pd.Timedelta(weeks=window_weeks)
    dates = pd.to_datetime(cases_df["date"])
    mask = (
        cases_df["region_id"].isin(child_ids)
        & (dates > cutoff_start)
        & (dates <= as_of_date)
    )
    totals = cases_df.loc[mask].groupby("region_id")["case_count"].sum()
    totals = totals.reindex(child_ids, fill_value=0.0)
    grand_total = float(totals.sum())
    if grand_total <= 0:
        log.debug(
            "compute_shares: %s has 0 cases in window, using uniform shares for %d children",
            parent_id,
            len(child_ids),
        )
        return {cid: 1.0 / len(child_ids) for cid in child_ids}
    return {cid: float(totals[cid]) / grand_total for cid in child_ids}


def downscale_predictions(
    parent_preds: pd.DataFrame,
    child_mapping: dict[str, str],
    cases_df: pd.DataFrame,
    as_of_date: pd.Timestamp,
    window_weeks: int,
) -> pd.DataFrame:
    """Disaggregate parent-level predictions to child level.

    parent_preds expected columns:
        dateOfComputingPrediction, startDatePredictedWeek, regionID,
        prediction, thresholdMethod, predictionZone, model

    Returns a DataFrame with the same columns but child-level regionIDs.
    Each child prediction = parent prediction × child's share.
    predictionZone and thresholdMethod are inherited from the parent row.
    Parent rows with no children in child_mapping are dropped with a warning.
    """
    _COLS = [
        "dateOfComputingPrediction",
        "startDatePredictedWeek",
        "regionID",
        "prediction",
        "thresholdMethod",
        "predictionZone",
        "model",
    ]

    parent_to_children: dict[str, list[str]] = {}
    for child_id, parent_id in child_mapping.items():
        parent_to_children.setdefault(parent_id, []).append(child_id)

    shares_by_parent: dict[str, dict[str, float]] = {
        parent_id: compute_shares(
            cases_df, parent_id, child_ids, as_of_date, window_weeks
        )
        for parent_id, child_ids in parent_to_children.items()
    }

    rows = []
    unknown_parents: set[str] = set()
    for _, row in parent_preds.iterrows():
        parent_id = row["regionID"]
        if parent_id not in shares_by_parent:
            unknown_parents.add(parent_id)
            continue
        for child_id, share in shares_by_parent[parent_id].items():
            rows.append(
                {
                    "dateOfComputingPrediction": row["dateOfComputingPrediction"],
                    "startDatePredictedWeek": row["startDatePredictedWeek"],
                    "regionID": child_id,
                    "prediction": float(row["prediction"]) * share,
                    "thresholdMethod": row["thresholdMethod"],
                    "predictionZone": row["predictionZone"],
                    "model": row["model"],
                }
            )

    if unknown_parents:
        log.warning(
            "downscale_predictions: %d parent region(s) have no children in mapping: %s",
            len(unknown_parents),
            sorted(unknown_parents),
        )

    return pd.DataFrame(rows, columns=_COLS) if rows else pd.DataFrame(columns=_COLS)
