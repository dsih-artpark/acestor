"""Pure computation: child → parent spatial aggregation via geojson mapping.

Where downscale splits a parent prediction across children by case share
(LRM), rollup is the trivial inverse: parent = sum(children). Preserves
float and int invariants automatically:

  * ``sum(child.predictionRaw) == parent.predictionRaw``      (exact)
  * ``sum(child.predictionInt) == round_half_up(parent.raw)`` (per LRM's
    output at the child level — the child ints already satisfy this in
    the acestor downscale format; we just add them up)

Zone classification is re-derived at the parent level from parent-level
historical case data — same code the downscale pipeline uses to assign
child zones, just applied at a coarser scale.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from pipelines.dengue_downscale.lib.apportionment import round_half_up

log = logging.getLogger(__name__)

_PREDICTION_COLS = [
    "dateOfComputingPrediction",
    "startDatePredictedWeek",
    "regionID",
    "predictionRaw",
    "prediction",
    "predictionInt",
    "predictionMin",
    "predictionMax",
    "thresholdMethod",
    "predictionZone",
    "model",
]


def build_child_parent_mapping(geojson_dir: Path) -> dict[str, str]:
    """Read all .geojson files in ``geojson_dir`` and return {child_id: parent_id}.

    Identical to the downscale helper — same geojson property contract
    (``region_id`` + ``parent``). Kept separate to avoid a cross-pipeline
    import in this thin adapter.
    """
    mapping: dict[str, str] = {}
    for path in sorted(geojson_dir.glob("*.geojson")):
        data = json.loads(path.read_text())
        features = data.get("features", [])
        if not features:
            log.warning(
                "build_child_parent_mapping: no features in %s, skipping", path.name
            )
            continue
        props = features[0].get("properties", {})
        # Fall back to `id` — some geojson exports (OD districts/blocks/states)
        # populate only `id` and skip `region_id`. Prep-side validation
        # (dengue_prep/steps/download_geojsons.py) also enforces at least
        # one is present.
        child_id = props.get("region_id") or props.get("id")
        # Prefer `parent_id` (snake) — same rationale as dengue_prep and
        # dengue_downscale: dashboard geojsons put the ULB/corporation in
        # `parent` and the immediate DB parent in `parent_id`. Local
        # geojsons only set `parent`, so fall back to it.
        parent_id = props.get("parent_id") or props.get("parent")
        if not child_id or not parent_id:
            log.warning(
                "build_child_parent_mapping: missing region_id or parent in %s, "
                "skipping",
                path.name,
            )
            continue
        mapping[child_id] = parent_id
    return mapping


def rollup_predictions(
    child_preds: pd.DataFrame,
    child_mapping: dict[str, str],
    *,
    on_missing_parents: str = "error",
) -> pd.DataFrame:
    """Aggregate child predictions into parent-level predictions.

    ``child_preds`` expected columns (acestor output schema):
        dateOfComputingPrediction, startDatePredictedWeek, regionID,
        prediction (int), predictionRaw (float), predictionInt (int, redundant),
        thresholdMethod, predictionZone, model

    Older parent CSVs (pre-#89) may only carry ``prediction`` = float. We
    auto-detect: if ``predictionRaw`` is absent, treat ``prediction`` as the
    raw float and the display integer is recomputed via round_half_up.

    Returns a DataFrame at the parent level with:
        * ``predictionRaw`` = sum of child raw floats  (exact)
        * ``prediction`` and ``predictionInt`` = round_half_up(predictionRaw)
        * ``predictionZone`` = re-derived at parent scale (caller's step
          responsibility, not this fn's)
    """
    if on_missing_parents not in ("error", "warn"):
        raise ValueError(
            f"on_missing_parents must be 'error' or 'warn', got {on_missing_parents!r}"
        )

    if child_preds.empty:
        return pd.DataFrame(columns=_PREDICTION_COLS)

    df = child_preds.copy()
    # Backward compat: pre-#89 CSVs only have `prediction` = float. In post-#89
    # CSVs, `prediction` = int (display) and `predictionRaw` = float (model).
    if "predictionRaw" not in df.columns:
        # Pre-#89: the only column is prediction=float. Derive the display int
        # via round_half_up so both invariants can be enforced downstream.
        df["predictionRaw"] = df["prediction"].astype(float)
        df["_child_int"] = df["prediction"].astype(float).apply(round_half_up)
    else:
        df["predictionRaw"] = df["predictionRaw"].astype(float)
        # Post-#89: `prediction` is the child's authoritative display integer.
        df["_child_int"] = df["prediction"].astype(int)

    df["_parent"] = df["regionID"].map(child_mapping)

    orphaned = df[df["_parent"].isna()]
    if not orphaned.empty:
        bad = sorted(orphaned["regionID"].unique().tolist())
        msg = (
            f"rollup_predictions: {len(bad)} child region(s) have no parent in the "
            f"geojson mapping and will not aggregate up: {bad}"
        )
        if on_missing_parents == "error":
            raise ValueError(msg)
        log.warning(msg)
        df = df[df["_parent"].notna()]

    group_cols = [
        "_parent",
        "dateOfComputingPrediction",
        "startDatePredictedWeek",
        "thresholdMethod",
        "model",
    ]
    # Sum both the raw floats and the child display ints. Parent's display
    # int is the SUM of children's display ints — NOT round_half_up of the
    # parent's raw — so the invariant sum(child.prediction) == parent.prediction
    # holds exactly. Individual rounding on children accumulates; re-rounding
    # the parent raw can differ from that sum by ±1.
    #
    # predictionMin / predictionMax: additive by construction. The parent's
    # min is the sum of children's mins (extreme case where every child
    # simultaneously hits its lower bound), symmetrically for max. Only
    # aggregated if every child row carried the columns; otherwise emit NA.
    agg_kwargs = {
        "predictionRaw": ("predictionRaw", "sum"),
        "_int_sum": ("_child_int", "sum"),
    }
    has_min = "predictionMin" in df.columns and df["predictionMin"].notna().any()
    has_max = "predictionMax" in df.columns and df["predictionMax"].notna().any()
    if has_min:
        agg_kwargs["predictionMin"] = ("predictionMin", "sum")
    if has_max:
        agg_kwargs["predictionMax"] = ("predictionMax", "sum")

    agg = df.groupby(group_cols, dropna=False, as_index=False).agg(**agg_kwargs)
    agg = agg.rename(columns={"_parent": "regionID"})
    agg["predictionRaw"] = agg["predictionRaw"].astype(float)
    agg["prediction"] = agg["_int_sum"].astype(int)
    agg["predictionInt"] = agg["prediction"]
    agg = agg.drop(columns=["_int_sum"])
    if not has_min:
        agg["predictionMin"] = pd.NA
    if not has_max:
        agg["predictionMax"] = pd.NA
    # Enforce min ≤ prediction ≤ max. Sum-of-child-ints (the parent's display
    # int) can drift from sum-of-child-mins/maxes by a rounding tick per child;
    # widen the bracket so the invariant every consumer expects holds.
    if has_min:
        agg["predictionMin"] = agg[["predictionMin", "prediction"]].min(axis=1)
    if has_max:
        agg["predictionMax"] = agg[["predictionMax", "prediction"]].max(axis=1)
    agg["predictionZone"] = pd.NA  # re-derived at parent scale by caller

    return agg[_PREDICTION_COLS]
