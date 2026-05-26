"""Pure computation: parent→child spatial disaggregation via rolling case shares."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_PREDICTION_COLS = [
    "dateOfComputingPrediction",
    "startDatePredictedWeek",
    "regionID",
    "prediction",
    "thresholdMethod",
    "predictionZone",
    "model",
]

_REQUIRED_CASE_COLS = ("region_id", "date", "case_count")

# Threshold method name (camelCase from prediction CSVs or snake_case) → param fn.
# Resolved lazily to avoid importing the dengue pipeline at module load.
_ZONE_METHOD_ALIASES = {
    "historical": "historical",
    "prev_nweeks": "prev_nweeks",
    "previousNweeks": "prev_nweeks",
    "weighted_baseline": "weighted_baseline",
    "weightedBaseline": "weighted_baseline",
}


def build_parent_child_mapping(geojson_dir: Path) -> dict[str, str]:
    """Read all .geojson files in geojson_dir and return {child_id: parent_id}.

    Each geojson must be a FeatureCollection whose first feature's properties
    contain 'region_id' and 'parent'. Files missing either field are skipped.

    Note: We only read features[0] because in this dataset, each geojson file
    represents a single region (one feature per file), so index 0 is the only feature.
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


def validate_cases_df(cases_df: pd.DataFrame) -> None:
    """Reject child-case frames we can't downscale from, instead of degrading silently.

    Raises ValueError on: empty frame, missing required columns, or no parseable dates.
    """
    if cases_df.empty:
        raise ValueError("validate_cases_df: child cases frame is empty")
    missing = [c for c in _REQUIRED_CASE_COLS if c not in cases_df.columns]
    if missing:
        raise ValueError(
            f"validate_cases_df: child cases frame missing columns {missing}; "
            f"required: {list(_REQUIRED_CASE_COLS)}"
        )
    parsed_dates = pd.to_datetime(cases_df["date"], errors="coerce")
    if parsed_dates.notna().sum() == 0:
        raise ValueError(
            "validate_cases_df: no parseable values in 'date' column (all NaT)"
        )
    if parsed_dates.isna().any():
        raise ValueError(
            "validate_cases_df: found unparseable value(s) in 'date' column"
        )


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
    Falls back to uniform shares (with a WARNING) when total cases in window == 0.
    Raises ValueError on NaN case counts inside the window — a data-integrity problem,
    not something to silently treat as zero.
    """
    cutoff_start = as_of_date - pd.Timedelta(weeks=window_weeks)
    dates = pd.to_datetime(cases_df["date"])
    mask = (
        cases_df["region_id"].isin(child_ids)
        & (dates > cutoff_start)
        & (dates <= as_of_date)
    )
    windowed = cases_df.loc[mask]

    if windowed["case_count"].isna().any():
        bad = sorted(windowed.loc[windowed["case_count"].isna(), "region_id"].unique())
        raise ValueError(
            f"compute_shares: NaN case_count in window for parent {parent_id} "
            f"(children {bad}) — refusing to treat missing data as zero"
        )

    present = set(cases_df["region_id"].unique())
    absent = [c for c in child_ids if c not in present]
    if absent:
        log.warning(
            "compute_shares: %d child region(s) absent from cases data for parent %s "
            "(will receive 0 share): %s",
            len(absent),
            parent_id,
            sorted(absent),
        )

    totals = windowed.groupby("region_id")["case_count"].sum()
    totals = totals.reindex(child_ids, fill_value=0.0)
    negative = totals[totals < 0]
    if not negative.empty:
        log.warning(
            "compute_shares: negative case counts for parent %s in children %s — clamping to 0",
            parent_id,
            sorted(negative.index.tolist()),
        )
        totals = totals.clip(lower=0)
    grand_total = float(totals.sum())
    if grand_total <= 0:
        log.warning(
            "compute_shares: parent %s has 0 cases in the %d-week window — "
            "falling back to uniform shares across %d children (downscaled split is "
            "not data-driven for this parent)",
            parent_id,
            window_weeks,
            len(child_ids),
        )
        return {cid: 1.0 / len(child_ids) for cid in child_ids}
    return {cid: float(totals[cid]) / grand_total for cid in child_ids}


def _child_threshold_params(canonical_method, cases_df, ctx):
    """Compute child-level (Mean, StdDev) per region/date for one threshold method.

    ctx is a dengue ThresholdContext carrying the same per-method knobs the parent
    run used (n_weeks, historical_n_years, recent_weeks, …) — supplied from config.
    """
    from pipelines.dengue.lib import thresholds as _th

    cdf = cases_df.rename(columns={"case_count": "case"})[
        ["region_id", "date", "case"]
    ].copy()
    cdf["date"] = pd.to_datetime(cdf["date"])
    if canonical_method == "historical":
        return _th.historical_threshold_params(
            cdf,
            n_years=ctx.historical_n_years,
            excluded_years=ctx.excluded_years,
            included_years=ctx.included_years,
        )
    if canonical_method == "prev_nweeks":
        return _th.prev_nweeks_threshold_params(cdf, n=ctx.n_weeks)
    if canonical_method == "weighted_baseline":
        return _th.weighted_baseline_threshold_params(
            cdf,
            recent_weeks=ctx.recent_weeks,
            sd_window_weeks=ctx.sd_window_weeks,
            weight_recent=ctx.weight_recent,
            weight_seasonal=ctx.weight_seasonal,
        )
    raise ValueError(f"unsupported threshold method {canonical_method!r}")


def _who_child_zones(child_preds, cases_df, as_of_date, list_alpha, ctx_by_method):
    """WHO bands per child: classify each child against its OWN T_α = Mean + α·StdDev.

    The threshold method is taken per row from the parent's ``thresholdMethod`` so
    children stay consistent with whichever method the parent run selected.
    """
    from pipelines.dengue.lib import zones as _zones

    canon = child_preds["thresholdMethod"].map(_ZONE_METHOD_ALIASES.get)
    unknown = sorted(set(child_preds.loc[canon.isna(), "thresholdMethod"]))
    if unknown:
        raise ValueError(
            f"assign_child_zones: unknown thresholdMethod(s) {unknown} in predictions; "
            f"known: {sorted(set(_ZONE_METHOD_ALIASES))}"
        )

    out = pd.Series(0.0, index=child_preds.index, dtype="float64")
    for method, idx in canon.groupby(canon).groups.items():
        if method not in ctx_by_method:
            raise ValueError(
                f"assign_child_zones: predictions use threshold method {method!r} but it "
                f"is not in the downscale config's thresholds.methods "
                f"({sorted(ctx_by_method)}) — add it so child thresholds match the parent run"
            )
        params = _child_threshold_params(method, cases_df, ctx_by_method[method])
        params = params[pd.to_datetime(params["date"]) <= as_of_date]
        latest = params.sort_values("date").groupby("region_id", as_index=False).tail(1)

        leveled = _zones._add_threshold_levels(latest, list(list_alpha))
        pairs = _zones.ret_threshold_pairs(leveled)
        t_cols = [c for c in leveled.columns if c.startswith("T") and "." in c]
        keep = ["region_id", "Mean", "StdDev", "Zero", "Inf"] + t_cols

        sub = child_preds.loc[idx, ["regionID", "prediction"]].merge(
            leveled[keep], left_on="regionID", right_on="region_id", how="left"
        )
        sub["predictionZone"] = pd.NA
        sub = _zones.assign_zone(sub, pairs)
        # No usable threshold (no history, or degenerate Mean=0 & StdDev=0) → zone 0,
        # so a 0-case child can never carry an inherited elevated risk zone.
        degenerate = (sub["Mean"].fillna(0) == 0) & (sub["StdDev"].fillna(0) == 0)
        sub.loc[degenerate, "predictionZone"] = pd.NA
        sub_zone = pd.to_numeric(sub["predictionZone"], errors="coerce").fillna(0.0)
        out.loc[idx] = sub_zone.to_numpy(dtype="float64")
    return out


def assign_child_zones(
    child_preds: pd.DataFrame,
    cases_df: pd.DataFrame,
    as_of_date: pd.Timestamp,
    *,
    list_alpha,
    classification_method: str,
    ctx_by_method: dict,
) -> pd.Series:
    """Re-derive each child's risk zone from child-level data, never inherited.

    classification_method mirrors the dengue pipeline:
      - "who": per-region threshold bands T_α = Mean + α·StdDev, using the parent
        row's thresholdMethod and the per-method context from config.
      - "icmr": cross-sectional quartile strata across all children per predicted week.

    Returns a Series of zones aligned to ``child_preds.index``.
    """
    if child_preds.empty:
        return pd.Series([], dtype="float64")

    cp = child_preds.reset_index(drop=True)
    if classification_method == "who":
        zones = _who_child_zones(cp, cases_df, as_of_date, list_alpha, ctx_by_method)
    elif classification_method == "icmr":
        from pipelines.dengue.lib.thresholds import icmr_quartile_zones

        classified = icmr_quartile_zones(
            cp, prediction_col="prediction", date_col="startDatePredictedWeek"
        )
        zones = pd.to_numeric(
            classified.sort_index()["predictionZone"], errors="coerce"
        ).fillna(0.0)
    else:
        raise ValueError(
            f"classification_method must be 'who' or 'icmr', got {classification_method!r}"
        )
    zones.index = child_preds.index
    return zones


def check_numeric_sanity(
    parent_preds: pd.DataFrame,
    child_preds: pd.DataFrame | None,
    child_mapping: dict[str, str],
    *,
    rel_tol: float = 1e-6,
) -> None:
    """Fail fast on numerically nonsensical downscaling.

    - parent predictions must be finite and non-negative (catches upstream blowups
      like an exploding ensemble member before they propagate to children);
    - per (parent, predicted week), child predictions must sum back to the parent
      prediction (conservation) within a relative tolerance.
    """
    pp = pd.to_numeric(parent_preds["prediction"], errors="coerce")
    if not np.isfinite(pp).all():
        bad = sorted(parent_preds.loc[~np.isfinite(pp), "regionID"].unique())
        raise ValueError(
            f"check_numeric_sanity: non-finite parent prediction(s) for {bad}"
        )
    if (pp < 0).any():
        bad = sorted(parent_preds.loc[pp < 0, "regionID"].unique())
        raise ValueError(
            f"check_numeric_sanity: negative parent prediction(s) for {bad}"
        )

    if child_preds is None or child_preds.empty:
        return

    # AP parent CSVs carry one row per thresholdMethod, so (regionID, week) is NOT
    # unique — conservation must be keyed by thresholdMethod too. Children are
    # emitted once per parent row, so each (parent, week, method) group conserves to
    # that row's prediction.
    key = ["regionID", "startDatePredictedWeek", "thresholdMethod"]
    if parent_preds.duplicated(key).any():
        dup = parent_preds.loc[parent_preds.duplicated(key, keep=False), key]
        raise ValueError(
            f"check_numeric_sanity: parent predictions not unique on {key} — "
            f"can't key conservation (add 'model' if multiple models share the CSV). "
            f"Example dups: {dup.head(3).to_dict('records')}"
        )

    child = child_preds.copy()
    child["_parent"] = child["regionID"].map(child_mapping)
    csum = child.groupby(
        ["_parent", "startDatePredictedWeek", "thresholdMethod"]
    )["prediction"].sum()
    parent_lookup = parent_preds.set_index(key)["prediction"]
    for (parent_id, week, method), got in csum.items():
        expected = parent_lookup.get((parent_id, week, method))
        if expected is None or pd.isna(expected):
            continue
        expected = float(expected)
        tol = max(abs(expected) * rel_tol, 1e-6)
        if abs(got - expected) > tol:
            raise ValueError(
                f"check_numeric_sanity: conservation violated for parent {parent_id} "
                f"week {week} method {method} — child sum {got:.6g} != "
                f"parent {expected:.6g}"
            )


def downscale_diagnostics(
    parent_preds: pd.DataFrame,
    child_preds: pd.DataFrame,
    child_mapping: dict[str, str],
) -> dict:
    """Descriptive sanity report on a downscale result (for logging/traceability).

    - n_parents_uniform: parents whose children share a week with ~zero spread
      (i.e. an even split — typically the no-data uniform fallback, not heterogeneity);
    - parent-vs-child risk: per (parent, week), how the highest child zone compares
      to the parent's own zone (below / above / matching).
    """
    if child_preds.empty:
        return {
            "n_parent_weeks": 0,
            "n_parents_uniform": 0,
            "n_weeks_children_below_parent": 0,
            "n_weeks_children_above_parent": 0,
            "n_weeks_zone_match": 0,
            "conservation_max_abs_err": 0.0,
        }

    # Keyed by thresholdMethod (AP parent rows repeat per method; zones differ by method).
    gkey = ["_parent", "startDatePredictedWeek", "thresholdMethod"]
    pkey = ["regionID", "startDatePredictedWeek", "thresholdMethod"]
    child = child_preds.copy()
    child["_parent"] = child["regionID"].map(child_mapping)
    grouped = child.groupby(gkey)

    spread = grouped["prediction"].agg(lambda s: float(np.std(s.to_numpy(), ddof=0)))
    n_parents_uniform = int(
        spread[spread <= 1e-9].index.get_level_values("_parent").nunique()
    )

    max_child_zone = grouped["predictionZone"].max()
    parent_zone = parent_preds.set_index(pkey)["predictionZone"]
    parent_pred = parent_preds.set_index(pkey)["prediction"]
    child_sum = grouped["prediction"].sum()

    below = above = match = 0
    max_cons_err = 0.0
    for (parent_id, week, method), mcz in max_child_zone.items():
        pz = parent_zone.get((parent_id, week, method))
        if pz is not None and not pd.isna(pz) and not pd.isna(mcz):
            if mcz < pz:
                below += 1
            elif mcz > pz:
                above += 1
            else:
                match += 1
        pp = parent_pred.get((parent_id, week, method))
        if pp is not None and not pd.isna(pp):
            max_cons_err = max(
                max_cons_err,
                abs(float(child_sum[(parent_id, week, method)]) - float(pp)),
            )

    return {
        "n_parent_weeks": int(len(max_child_zone)),
        "n_parents_uniform": n_parents_uniform,
        "n_weeks_children_below_parent": below,
        "n_weeks_children_above_parent": above,
        "n_weeks_zone_match": match,
        "conservation_max_abs_err": float(max_cons_err),
    }


def downscale_predictions(
    parent_preds: pd.DataFrame,
    child_mapping: dict[str, str],
    cases_df: pd.DataFrame,
    as_of_date: pd.Timestamp,
    window_weeks: int,
    *,
    list_alpha,
    classification_method: str,
    ctx_by_method: dict,
    on_missing_parents: str = "error",
) -> pd.DataFrame:
    """Disaggregate parent-level predictions to child level.

    parent_preds expected columns:
        dateOfComputingPrediction, startDatePredictedWeek, regionID,
        prediction, thresholdMethod, predictionZone, model

    Returns a DataFrame with the same columns but child-level regionIDs.
    Each child prediction = parent prediction × child's share. predictionZone is
    re-derived per child via assign_child_zones (NOT inherited from the parent);
    list_alpha, classification_method, and ctx_by_method come from config so the
    child zones match whatever the parent run used.

    on_missing_parents controls what happens when a parent in parent_preds has no
    children in child_mapping:
      - "error" (default): raise — the predicted cases would otherwise vanish and
        child totals would not conserve.
      - "warn": log a warning and drop those parents' rows (legacy behaviour).
    """
    if on_missing_parents not in ("error", "warn"):
        raise ValueError(
            f"on_missing_parents must be 'error' or 'warn', got {on_missing_parents!r}"
        )

    validate_cases_df(cases_df)
    # Fail fast on non-finite/negative parent predictions before doing any work.
    check_numeric_sanity(parent_preds, None, child_mapping)

    parent_to_children: dict[str, list[str]] = {}
    for child_id, parent_id in child_mapping.items():
        parent_to_children.setdefault(parent_id, []).append(child_id)

    predicted_parents = set(parent_preds["regionID"].unique())
    missing = sorted(predicted_parents - set(parent_to_children))
    if missing:
        msg = (
            f"downscale_predictions: {len(missing)} predicted parent region(s) have no "
            f"children in the mapping, so their predictions cannot be disaggregated and "
            f"child totals would not conserve: {missing}"
        )
        if on_missing_parents == "error":
            raise ValueError(msg)
        log.warning(msg)

    shares_by_parent: dict[str, dict[str, float]] = {
        parent_id: compute_shares(
            cases_df, parent_id, child_ids, as_of_date, window_weeks
        )
        for parent_id, child_ids in parent_to_children.items()
    }

    rows = []
    for _, row in parent_preds.iterrows():
        parent_id = row["regionID"]
        if parent_id not in shares_by_parent:
            continue
        for child_id, share in shares_by_parent[parent_id].items():
            rows.append(
                {
                    "dateOfComputingPrediction": row["dateOfComputingPrediction"],
                    "startDatePredictedWeek": row["startDatePredictedWeek"],
                    "regionID": child_id,
                    "prediction": float(row["prediction"]) * share,
                    "thresholdMethod": row["thresholdMethod"],
                    "predictionZone": pd.NA,  # re-derived below, never inherited
                    "model": row["model"],
                }
            )

    if not rows:
        return pd.DataFrame(columns=_PREDICTION_COLS)

    child_preds = pd.DataFrame(rows, columns=_PREDICTION_COLS)
    child_preds["predictionZone"] = assign_child_zones(
        child_preds,
        cases_df,
        as_of_date,
        list_alpha=list_alpha,
        classification_method=classification_method,
        ctx_by_method=ctx_by_method,
    )
    check_numeric_sanity(parent_preds, child_preds, child_mapping)
    return child_preds
